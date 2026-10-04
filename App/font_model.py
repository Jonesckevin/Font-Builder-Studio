"""Font Builder Studio - the font document model.

The document is the contract between the browser editor and the font engine.
The browser owns it; the server only serializes/deserializes it around
FontForge (see ``font_worker.py``).

Shape (JSON)::

    {
      "version": 1,
      "em": 1000, "ascent": 800, "descent": 200,
      "family_name": "Untitled", "style_name": "Regular", "font_name": "...",
      "comment": "",
      "glyphs": [
        {
          "name": "A", "unicode": 65,
          "width": 600, "lsb": 0, "vwidth": 0,
          "quadratic": false,
          "contours": [
            {"closed": true, "quadratic": false,
             "points": [[x, y, on_curve, type, interpolated, name], ...]}
          ],
          "references": [["Aacute", [1, 0, 0, 1, 0, 0], false], ...]
        }
      ]
    }

Points are **arrays**, not objects: a large font holds millions of points and
per-point object overhead would be prohibitive.

``type`` is a fontforge point type:
0 = corner, 1 = curve, 2 = hv-curve, 3 = tangent
(``fontforge.splineCorner`` / ``splineCurve`` / ``splineHVCurve`` / ``splineTangent``).
"""

from __future__ import annotations

import os

DOC_VERSION = 1

POINT_CORNER = 0
POINT_CURVE = 1
POINT_HV_CURVE = 2
POINT_TANGENT = 3
VALID_POINT_TYPES = frozenset({POINT_CORNER, POINT_CURVE, POINT_HV_CURVE, POINT_TANGENT})

# --- Limits (protect the engine from hostile or accidental input) -----------
MAX_GLYPHS = 65535
MAX_CONTOURS_PER_GLYPH = 4096
MAX_POINTS_PER_CONTOUR = 8192
MAX_TOTAL_POINTS = 4_000_000
MAX_COORD = 1_000_000
MAX_UNICODE = 0x10FFFF
MAX_NAME_LEN = 256
MAX_TEXT_LEN = 4096

# Formats the engine can produce, and how fontforge writes each one.
GENERATE_FORMATS = frozenset({"ttf", "otf", "woff", "woff2", "svg"})
SAVE_FORMATS = frozenset({"sfd", "sfdir"})
SUPPORTED_FORMATS = GENERATE_FORMATS | SAVE_FORMATS

#: Content type to serve each format as.
FORMAT_MIME = {
    "ttf": "font/ttf",
    "otf": "font/otf",
    "woff": "font/woff",
    "woff2": "font/woff2",
    "sfd": "application/x-font-sfd",
    "sfdir": "application/zip",
    "svg": "image/svg+xml",
}

#: File types accepted for upload. `.ufo`/`.sfdir` are directories and must
#: arrive zipped; `.ttc` is a font collection.
UPLOAD_EXTENSIONS = frozenset(
    {
        ".sfd",
        ".sfdir",
        ".ttf",
        ".otf",
        ".ttc",
        ".woff",
        ".woff2",
        ".ufo",
        ".ufoz",
        ".zip",
        ".pfb",
        ".pfa",
        ".svg",
    }
)


class DocumentError(ValueError):
    """Raised when a font document is malformed or exceeds a limit."""


def empty_document(
    em: int = 1000,
    ascent: int = 800,
    descent: int = 200,
    family_name: str = "Untitled",
    style_name: str = "Regular",
) -> dict:
    """A valid, empty font document ready for the editor to populate."""
    return {
        "version": DOC_VERSION,
        "em": em,
        "ascent": ascent,
        "descent": descent,
        "family_name": family_name,
        "style_name": style_name,
        "font_name": f"{family_name.replace(' ', '')}-{style_name.replace(' ', '')}",
        "comment": "",
        "glyphs": [],
    }


def new_glyph(name: str, unicode_: int = -1) -> dict:
    """An empty glyph record."""
    return {
        "name": name,
        "unicode": unicode_,
        "width": 0,
        "lsb": 0,
        "vwidth": 0,
        "quadratic": False,
        "contours": [],
        "references": [],
    }


def document_stats(doc: dict) -> dict:
    """Cheap counters used for logging and UI summaries."""
    glyphs = doc.get("glyphs") or []
    contours = sum(len(g.get("contours") or ()) for g in glyphs)
    points = sum(
        len(c.get("points") or ()) for g in glyphs for c in (g.get("contours") or ())
    )
    encoded = sum(1 for g in glyphs if isinstance(g.get("unicode"), int) and g["unicode"] >= 0)
    return {
        "glyphs": len(glyphs),
        "encoded_glyphs": encoded,
        "contours": contours,
        "points": points,
        "references": sum(len(g.get("references") or ()) for g in glyphs),
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DocumentError(message)


def _check_int(value, label: str, lo: int, hi: int) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool), f"{label} must be an integer")
    _require(lo <= value <= hi, f"{label} out of range [{lo}, {hi}]: {value}")
    return value


def _check_number(value, label: str) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool), f"{label} must be a number")
    _require(value == value, f"{label} must not be NaN")
    _require(abs(value) <= MAX_COORD, f"{label} out of range: {value}")
    return float(value)


def _check_text(value, label: str, max_len: int = MAX_TEXT_LEN) -> str:
    _require(isinstance(value, str), f"{label} must be a string")
    _require(len(value) <= max_len, f"{label} too long (max {max_len})")
    return value


def validate_document(doc: object) -> dict:
    """Validate a font document, raising :class:`DocumentError` on any problem.

    Validation is deliberately strict and complete: the engine is a C library
    with a history of memory-safety bugs, so nothing reaches it unchecked.
    """
    _require(isinstance(doc, dict), "document must be an object")

    version = doc.get("version", DOC_VERSION)
    _require(version == DOC_VERSION, f"unsupported document version: {version!r}")

    _check_int(doc.get("em", 1000), "em", 1, 16384)
    _check_int(doc.get("ascent", 800), "ascent", -MAX_COORD, MAX_COORD)
    _check_int(doc.get("descent", 200), "descent", -MAX_COORD, MAX_COORD)

    for key in ("family_name", "style_name", "font_name"):
        if key in doc:
            _check_text(doc[key], key, MAX_NAME_LEN)
    if "comment" in doc:
        _check_text(doc["comment"], "comment")

    glyphs = doc.get("glyphs")
    _require(isinstance(glyphs, list), "glyphs must be a list")
    _require(len(glyphs) <= MAX_GLYPHS, f"too many glyphs (max {MAX_GLYPHS})")

    total_points = 0
    seen_names: set[str] = set()

    for gi, glyph in enumerate(glyphs):
        where = f"glyphs[{gi}]"
        _require(isinstance(glyph, dict), f"{where} must be an object")

        name = glyph.get("name")
        _require(isinstance(name, str) and name != "", f"{where}.name must be a non-empty string")
        _require(len(name) <= MAX_NAME_LEN, f"{where}.name too long")
        _require(name not in seen_names, f"duplicate glyph name: {name!r}")
        seen_names.add(name)

        _check_int(glyph.get("unicode", -1), f"{where}.unicode", -1, MAX_UNICODE)
        _check_int(glyph.get("width", 0), f"{where}.width", -MAX_COORD, MAX_COORD)
        _check_int(glyph.get("lsb", 0), f"{where}.lsb", -MAX_COORD, MAX_COORD)
        _check_int(glyph.get("vwidth", 0), f"{where}.vwidth", -MAX_COORD, MAX_COORD)

        contours = glyph.get("contours") or []
        _require(isinstance(contours, list), f"{where}.contours must be a list")
        _require(
            len(contours) <= MAX_CONTOURS_PER_GLYPH,
            f"{where} has too many contours (max {MAX_CONTOURS_PER_GLYPH})",
        )

        for ci, contour in enumerate(contours):
            cwhere = f"{where}.contours[{ci}]"
            _require(isinstance(contour, dict), f"{cwhere} must be an object")
            _require(isinstance(contour.get("closed", True), bool), f"{cwhere}.closed must be a boolean")
            _require(
                isinstance(contour.get("quadratic", False), bool),
                f"{cwhere}.quadratic must be a boolean",
            )

            points = contour.get("points")
            _require(isinstance(points, list), f"{cwhere}.points must be a list")
            _require(len(points) >= 2, f"{cwhere} must have at least 2 points")
            _require(
                len(points) <= MAX_POINTS_PER_CONTOUR,
                f"{cwhere} has too many points (max {MAX_POINTS_PER_CONTOUR})",
            )
            total_points += len(points)
            _require(total_points <= MAX_TOTAL_POINTS, "document has too many points in total")

            for pi, point in enumerate(points):
                pwhere = f"{cwhere}.points[{pi}]"
                _require(isinstance(point, (list, tuple)), f"{pwhere} must be an array")
                _require(len(point) >= 3, f"{pwhere} needs at least [x, y, on_curve]")
                _check_number(point[0], f"{pwhere}.x")
                _check_number(point[1], f"{pwhere}.y")
                _require(point[2] in (0, 1, True, False), f"{pwhere}.on_curve must be 0 or 1")
                if len(point) > 3 and point[3] is not None:
                    _require(
                        point[3] in VALID_POINT_TYPES,
                        f"{pwhere}.type must be one of {sorted(VALID_POINT_TYPES)}",
                    )
                if len(point) > 5 and point[5] is not None:
                    _check_text(point[5], f"{pwhere}.name", MAX_NAME_LEN)

        references = glyph.get("references") or []
        _require(isinstance(references, list), f"{where}.references must be a list")
        for ri, ref in enumerate(references):
            rwhere = f"{where}.references[{ri}]"
            _require(isinstance(ref, (list, tuple)) and len(ref) >= 1, f"{rwhere} must be [name, matrix, selected]")
            _check_text(ref[0], f"{rwhere}.name", MAX_NAME_LEN)
            if len(ref) > 1 and ref[1] is not None:
                matrix = ref[1]
                _require(
                    isinstance(matrix, (list, tuple)) and len(matrix) == 6,
                    f"{rwhere}.matrix must have 6 numbers",
                )
                for mi, value in enumerate(matrix):
                    _check_number(value, f"{rwhere}.matrix[{mi}]")

    return doc


def normalise_format(fmt: str) -> str:
    """Normalise a user-supplied format/extension to a supported format name."""
    fmt = (fmt or "").strip().lower().lstrip(".")
    if fmt == "truetype":
        fmt = "ttf"
    if fmt == "cff":
        fmt = "otf"
    if fmt == "opentype":
        fmt = "otf"
    if fmt not in SUPPORTED_FORMATS:
        raise DocumentError(
            f"unsupported format {fmt!r}; expected one of {sorted(SUPPORTED_FORMATS)}"
        )
    return fmt


def extension_of(filename: str) -> str:
    """Lower-cased extension including the dot (``''`` when there is none)."""
    return os.path.splitext(filename or "")[1].lower()


def is_allowed_upload(filename: str) -> bool:
    """Whether an upload's extension is one we are willing to hand to the engine."""
    return extension_of(filename) in UPLOAD_EXTENSIONS
