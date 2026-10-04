"""Font Builder Studio - font document API.

Endpoints::

    GET  /api/font/formats   supported formats, upload types and limits
    POST /api/font/new       create an empty document
    POST /api/font/parse     upload a font  -> document JSON
    POST /api/font/build     document JSON  -> font file (download)

All FontForge work goes through :mod:`font_engine`, which shells out to a
one-shot worker process. **Nothing here imports FontForge**.
"""

from __future__ import annotations

import io
import json
import logging

from flask import Blueprint, current_app, jsonify, request, send_file
from werkzeug.utils import secure_filename

import font_engine
import font_model
import unicode_data
from extensions import limiter

try:  # libmagic is in the image (libmagic1); degrade gracefully without it
    import magic as _magic
except Exception:  # noqa: BLE001
    _magic = None

log = logging.getLogger("fontbuilder.api")

bp = Blueprint("font_api", __name__, url_prefix="/api/font")

#: Formats that can be downloaded directly. `sfdir` saves a *directory*, which
#: cannot be streamed as a single file.
HTTP_UNSUPPORTED_FORMATS = frozenset({"sfdir"})

#: Content types that are never a font. Extensions are trivially spoofed, so the
#: bytes are sniffed as well. libmagic reports many real fonts as
#: application/octet-stream (or unknown), so only positively *dangerous* types
#: are rejected here - the font engine remains the real validator.
_DANGEROUS_MIME = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "application/javascript",
        "text/javascript",
        "text/x-python",
        "text/x-shellscript",
        "application/x-httpd-php",
        "application/x-executable",
        "application/x-sharedlib",
        "application/x-dosexec",
        "application/x-msdownload",
        "application/x-elf",
        "application/x-mach-binary",
    }
)


def _sniff_upload(data: bytes) -> str | None:
    """Return a rejection reason when the bytes are clearly not a font."""
    if _magic is None:
        return None
    try:
        mime = (_magic.from_buffer(data[:4096], mime=True) or "").lower()
    except Exception:  # noqa: BLE001 - never fail an upload over a sniff error
        return None
    if mime in _DANGEROUS_MIME:
        return f"uploaded content looks like {mime}, not a font"
    return None


def _error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def _bounded_int(payload: dict, key: str, default: int, lo: int, hi: int) -> int:
    value = payload.get(key, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise font_model.DocumentError(f"{key} must be an integer") from None
    if not lo <= value <= hi:
        raise font_model.DocumentError(f"{key} must be between {lo} and {hi}")
    return value


def _bounded_text(payload: dict, key: str, default: str) -> str:
    value = payload.get(key, default)
    if not isinstance(value, str):
        raise font_model.DocumentError(f"{key} must be a string")
    value = value.strip()
    return value[: font_model.MAX_NAME_LEN] if value else default


# ============================================================================
# Capabilities
# ============================================================================
@bp.get("/formats")
def list_formats():
    """Everything the editor needs to build its import/export UI."""
    max_upload = current_app.config.get("MAX_CONTENT_LENGTH") or 0
    return jsonify(
        {
            "ok": True,
            "export_formats": [
                {
                    "format": fmt,
                    "mimetype": font_model.FORMAT_MIME.get(fmt, "application/octet-stream"),
                    "extension": fmt,
                }
                for fmt in sorted(font_model.SUPPORTED_FORMATS - HTTP_UNSUPPORTED_FORMATS)
            ],
            "import_extensions": sorted(font_model.UPLOAD_EXTENSIONS),
            "mimetypes": font_model.FORMAT_MIME,
            "operations": font_engine.list_operations(),
            "unicode_picker": unicode_data.meta(),
            "limits": {
                "max_upload_bytes": max_upload,
                "max_glyphs": font_model.MAX_GLYPHS,
                "max_points_total": font_model.MAX_TOTAL_POINTS,
                "max_points_per_contour": font_model.MAX_POINTS_PER_CONTOUR,
                "max_contours_per_glyph": font_model.MAX_CONTOURS_PER_GLYPH,
            },
        }
    )


# ============================================================================
# New document
# ============================================================================
@bp.post("/new")
@limiter.limit("120 per minute")
def new_font():
    """Create an empty font document (the "new font from scratch" flow)."""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return _error("expected a JSON object")

    try:
        doc = font_model.empty_document(
            em=_bounded_int(payload, "em", 1000, 1, 16384),
            ascent=_bounded_int(payload, "ascent", 800, 0, font_model.MAX_COORD),
            descent=_bounded_int(payload, "descent", 200, 0, font_model.MAX_COORD),
            family_name=_bounded_text(payload, "family_name", "Untitled"),
            style_name=_bounded_text(payload, "style_name", "Regular"),
        )
        font_model.validate_document(doc)
    except font_model.DocumentError as exc:
        return _error(str(exc))

    return jsonify({"ok": True, "document": doc, "stats": font_model.document_stats(doc)})


# ============================================================================
# Parse (font -> document)
# ============================================================================
@bp.post("/parse")
@limiter.limit("30 per minute")
def parse_font():
    """Parse an uploaded font into an editable document.

    Send multipart/form-data with the font in the ``file`` field. Add
    ``?stats_only=1`` to omit the (potentially very large) glyph data.
    """
    upload = request.files.get("file") or request.files.get("font")
    if upload is None:
        return _error("no file uploaded - expected a multipart field named 'file'")

    filename = upload.filename or ""
    if not font_model.is_allowed_upload(filename):
        ext = font_model.extension_of(filename) or "(none)"
        return _error(
            f"unsupported font type {ext!r}; expected one of "
            f"{', '.join(sorted(font_model.UPLOAD_EXTENSIONS))}",
            415,
        )

    data = upload.read()
    if not data:
        return _error("uploaded file is empty")

    sniffed = _sniff_upload(data)
    if sniffed:
        log.warning("rejected upload %r: %s", filename, sniffed)
        return _error(sniffed, 415)

    try:
        doc = font_engine.parse_font_bytes(data, filename)
    except font_engine.EngineError as exc:
        log.warning("parse failed for %s: %s", filename, exc)
        return _error(f"could not parse font: {exc}", 422)

    stats = font_model.document_stats(doc)
    log.info("parsed %s -> %s", filename, stats)

    if request.args.get("stats_only") in ("1", "true", "yes"):
        return jsonify({"ok": True, "filename": filename, "stats": stats})

    return jsonify({"ok": True, "filename": filename, "document": doc, "stats": stats})


# ============================================================================
# Build (document -> font)
# ============================================================================
@bp.post("/build")
@limiter.limit("30 per minute")
def build_font():
    """Build a font file from a document and return it as a download.

    Body: ``{"document": {...}, "format": "ttf", "filename": "optional.ttf"}``
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("expected a JSON object with 'document' and 'format'")

    doc = payload.get("document")
    if not isinstance(doc, dict):
        return _error("missing 'document' object")

    try:
        fmt = font_model.normalise_format(payload.get("format") or "ttf")
    except font_model.DocumentError as exc:
        return _error(str(exc))

    if fmt in HTTP_UNSUPPORTED_FORMATS:
        return _error(
            f"format {fmt!r} saves a directory and cannot be downloaded; use 'sfd' instead"
        )

    try:
        data, stats = font_engine.build_font_bytes(doc, fmt)
    except font_model.DocumentError as exc:
        return _error(f"invalid document: {exc}")
    except font_engine.EngineError as exc:
        log.warning("build failed for %s: %s", fmt, exc)
        return _error(f"could not build font: {exc}", 422)

    download_name = secure_filename(str(payload.get("filename") or "")) or f"font.{fmt}"
    if not download_name.lower().endswith(f".{fmt}"):
        download_name = f"{download_name}.{fmt}"

    log.info("built %s (%s bytes) %s", download_name, len(data), stats)

    response = send_file(
        io.BytesIO(data),
        mimetype=font_model.FORMAT_MIME.get(fmt, "application/octet-stream"),
        as_attachment=True,
        download_name=download_name,
        max_age=0,
    )
    # Numbers only, so this is always header-safe.
    response.headers["X-Font-Stats"] = json.dumps(stats, separators=(",", ":"))
    return response


# ============================================================================
# Unicode character picker
# ============================================================================
# The data behind these routes is a plain gzipped index built at image build
# time (tools/build_unicode_index.py). FontForge is not imported to serve it,
# and a missing index degrades to "no picker" rather than an error.
@bp.get("/unicode/blocks")
def unicode_blocks():
    """Every Unicode block, so the picker can list them as sections."""
    blocks = unicode_data.blocks()
    return jsonify(
        {
            "ok": True,
            "meta": unicode_data.meta(),
            "blocks": blocks,
        }
    )


@bp.get("/unicode/chars")
@limiter.limit("240 per minute")
def unicode_chars():
    """Characters in one block, paginated: ``?block=0&offset=0&limit=256``."""
    raw_block = request.args.get("block", type=int)
    if raw_block is None:
        return _error("missing integer 'block' parameter")

    page = unicode_data.chars(
        raw_block,
        offset=request.args.get("offset", default=0, type=int) or 0,
        limit=request.args.get("limit", default=unicode_data.DEFAULT_LIMIT, type=int)
        or unicode_data.DEFAULT_LIMIT,
    )
    if page is None:
        return _error(f"unknown block index {raw_block}", 404)

    return jsonify({"ok": True, **page})


@bp.get("/unicode/search")
@limiter.limit("240 per minute")
def unicode_search():
    """Search characters by name, annotation or code point.

    ``?q=snowman`` and ``?q=U+2603`` both work. Responses are cached, so
    repeated keystrokes that resolve to the same query are free.
    """
    query = request.args.get("q", "") or ""
    if len(query) > 64:
        query = query[:64]
    limit = request.args.get("limit", default=unicode_data.DEFAULT_LIMIT, type=int)
    limit = max(1, min(limit or unicode_data.DEFAULT_LIMIT, unicode_data.MAX_LIMIT))

    if not unicode_data.available():
        return jsonify({"ok": True, "results": [], "total": 0, "available": False})

    cached = unicode_data.cached_search(query, limit)
    return current_app.response_class(cached, mimetype="application/json")


@bp.get("/unicode/lookup")
def unicode_lookup():
    """Name/metadata for a single code point: ``?code=9731`` or ``?code=U+2603``."""
    raw = request.args.get("code", "")
    code = unicode_data.parse_codepoint(raw)
    if code is None:
        return _error("missing or invalid 'code' parameter (try U+2603 or 9731)")

    entry = unicode_data.lookup(code)
    if entry is None:
        # Unnamed code points (controls, private use, unassigned) are valid to
        # encode but have nothing to look up - that is not an error.
        return jsonify(
            {
                "ok": True,
                "code": code,
                "name": "",
                "glyph_name": "",
                "named": False,
            }
        )
    return jsonify({"ok": True, "named": True, **entry})


# ============================================================================
# Operations (FontForge's own glyph cleanup tools)
# ============================================================================
@bp.post("/op")
@limiter.limit("30 per minute")
def run_operation():
    """Apply a FontForge operation to some or all glyphs.

    Body: ``{"document": {...}, "op": "removeOverlap", "glyphs": ["A"]}``

    ``glyphs`` is optional - omit it (or send ``null``) to operate on the whole
    font. Returns the updated document, which replaces the client's copy.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("expected a JSON object with 'document' and 'op'")

    doc = payload.get("document")
    if not isinstance(doc, dict):
        return _error("missing 'document' object")

    op = payload.get("op")
    if not isinstance(op, str) or not op:
        return _error("missing 'op' name")

    available = font_engine.list_operations()
    if op not in available:
        return _error(f"unknown operation {op!r}; expected one of {', '.join(available)}")

    glyphs = payload.get("glyphs")
    if glyphs is not None:
        if not isinstance(glyphs, list) or not all(isinstance(g, str) for g in glyphs):
            return _error("'glyphs' must be a list of glyph names")
        glyphs = glyphs[: font_model.MAX_GLYPHS] or None

    try:
        result, info = font_engine.apply_op(doc, op, glyphs)
    except font_model.DocumentError as exc:
        return _error(f"invalid document: {exc}")
    except font_engine.EngineError as exc:
        log.warning("operation %s failed: %s", op, exc)
        return _error(f"operation failed: {exc}", 422)

    return jsonify(
        {
            "ok": True,
            "op": op,
            "applied": info.get("applied"),
            "errors": info.get("errors", []),
            "document": result,
            "stats": font_model.document_stats(result),
        }
    )
