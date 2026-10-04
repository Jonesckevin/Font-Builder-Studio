"""Font Builder Studio - sandboxed, one-shot FontForge worker.

Runs as a **child process**, never inside the web worker. This is a correctness
requirement, not just crash containment: FontForge corrupts its process-global
state when several operations share an interpreter, producing
``Internal Error: Bad layer in _SCChngNoUpdate`` and SIGSEGVs..

Usage::

    python font_worker.py parse --input font.ttf   --output doc.json
    python font_worker.py build --input doc.json   --output font.ttf --format ttf

The envelope written to ``--output`` is always JSON::

    {"ok": true,  "op": "parse", "stats": {...}}
    {"ok": false, "op": "parse", "error": "...", "traceback": "..."}
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import traceback

import font_model


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------
#: The worker always writes its JSON envelope beside the requested output.
RESULT_SUFFIX = ".result.json"


def _write_result(path: str, payload: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def _fail(op: str, output: str, exc: BaseException) -> int:
    """Write a failure envelope beside the output path."""
    _write_result(
        f"{output}{RESULT_SUFFIX}",
        {
            "ok": False,
            "op": op,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-4000:],
        },
    )
    print(f"{op} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# parse: font file -> document
# ---------------------------------------------------------------------------
def _points_to_json(contour) -> list[list]:
    out = []
    for point in contour:
        out.append(
            [
                point.x,
                point.y,
                1 if point.on_curve else 0,
                getattr(point, "type", font_model.POINT_CURVE),
                1 if getattr(point, "interpolated", False) else 0,
                getattr(point, "name", None) or None,
            ]
        )
    return out


def parse_font(path: str) -> dict:
    """Load a font with FontForge and serialise it into a document."""
    import fontforge  # imported here so importing this module is cheap

    font = fontforge.open(path)
    try:
        doc = font_model.empty_document(
            em=getattr(font, "em", 1000) or 1000,
            ascent=getattr(font, "ascent", 800) or 0,
            descent=getattr(font, "descent", 200) or 0,
        )
        for attr, key in (
            ("familyname", "family_name"),
            ("stylename", "style_name"),
            ("fontname", "font_name"),
            ("comment", "comment"),
        ):
            value = getattr(font, attr, None)
            if isinstance(value, str) and value:
                doc[key] = value

        glyphs = []
        for glyph in font.glyphs():
            record = font_model.new_glyph(
                name=glyph.glyphname,
                unicode_=glyph.unicode if isinstance(glyph.unicode, int) else -1,
            )
            record["width"] = int(getattr(glyph, "width", 0) or 0)
            record["lsb"] = int(getattr(glyph, "left_side_bearing", 0) or 0)
            record["vwidth"] = int(getattr(glyph, "vwidth", 0) or 0)

            layer = glyph.foreground
            record["quadratic"] = bool(getattr(layer, "is_quadratic", False))

            contours = []
            for contour in layer:
                contours.append(
                    {
                        "closed": bool(contour.closed),
                        "quadratic": bool(getattr(contour, "is_quadratic", False)),
                        "points": _points_to_json(contour),
                    }
                )
            record["contours"] = contours

            # references are (name, (a,b,c,d,e,f), selected) tuples
            references = []
            for ref in glyph.references or ():
                try:
                    name, matrix, selected = ref[0], ref[1], ref[2]
                    references.append([name, [float(v) for v in matrix], bool(selected)])
                except (IndexError, TypeError, ValueError):
                    continue
            record["references"] = references

            glyphs.append(record)

        doc["glyphs"] = glyphs
        return doc
    finally:
        font.close()


# ---------------------------------------------------------------------------
# build: document -> font file
# ---------------------------------------------------------------------------
def _layer_order(layer, want_quadratic: bool) -> bool:
    """Return the contour order that contours must be built with.

    Adding a contour of the wrong order raises
    ``Both arguments must be Layers of the same order``.

    IMPORTANT: ``layer`` must be the *same object* the contours are then
    appended to. ``glyph.foreground`` returns a **copy**, so reading the order
    from one copy and appending to another reintroduces the exact mismatch this
    function exists to prevent.
    """
    try:
        if bool(layer.is_quadratic) != want_quadratic:
            layer.is_quadratic = want_quadratic
    except Exception:  # noqa: BLE001 - not settable in every build
        pass
    return bool(layer.is_quadratic)


def _ensure_glyph(font, unicode_, name: str):
    """Get or create a glyph by name.

    FontForge pre-creates a few glyphs in every font (``.notdef``, ``.null``,
    ``nonmarkingreturn``), and a document parsed from a font legitimately
    contains them. Creating one a second time fails, so an existing glyph is
    reused and remapped instead.
    """
    try:
        existing = font[name]
    except Exception:  # noqa: BLE001 - absent glyph raises
        existing = None

    if existing is not None:
        if isinstance(unicode_, int) and unicode_ >= 0:
            try:
                existing.unicode = unicode_
            except Exception:  # noqa: BLE001
                pass
        return existing

    try:
        if isinstance(unicode_, int) and unicode_ >= 0:
            return font.createChar(unicode_, name)
        return font.createChar(-1, name)
    except Exception:  # noqa: BLE001
        # e.g. the codepoint is already mapped to a different glyph
        return font.createChar(-1, name)


def _build_glyph(font, record: dict):
    import fontforge  # noqa: PLC0415

    unicode_ = record.get("unicode", -1)
    name = record["name"]
    glyph = _ensure_glyph(font, unicode_, name)

    # A single layer object is used for the whole operation - see _layer_order().
    layer = glyph.foreground
    order2 = _layer_order(layer, bool(record.get("quadratic", False)))

    for contour_data in record.get("contours") or ():
        points = contour_data.get("points") or ()
        contour = fontforge.contour(order2)

        # Slice-assign takes (x, y, on_curve[, type]) initialisers. Point names
        # are not representable in that form, so they are applied afterwards.
        tuples = []
        names = []
        for point in points:
            x, y = float(point[0]), float(point[1])
            on_curve = bool(point[2])
            ptype = point[3] if len(point) > 3 and point[3] is not None else None
            if ptype is None:
                tuples.append((x, y, on_curve))
            else:
                tuples.append((x, y, on_curve, ptype))
            names.append(point[5] if len(point) > 5 else None)

        contour[:] = tuples
        contour.closed = bool(contour_data.get("closed", True))

        for index, point_name in enumerate(names):
            if point_name:
                try:
                    contour[index].name = point_name
                except Exception:  # noqa: BLE001
                    break

        layer += contour

    glyph.foreground = layer

    for ref in record.get("references") or ():
        try:
            name_ = ref[0]
            matrix = tuple(ref[1]) if len(ref) > 1 and ref[1] else (1, 0, 0, 1, 0, 0)
            glyph.addReference(name_, matrix)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: skipping reference {ref!r}: {exc}", file=sys.stderr)

    # Order matters. Setting the left sidebearing slides the outline and drags the
    # advance width with it (keeping the right sidebearing), so the width has to
    # be applied *after* it - otherwise the built font quietly ends up with
    # width + (lsb - xMin) instead of the width the document asked for.
    try:
        glyph.left_side_bearing = int(record.get("lsb", 0) or 0)
    except Exception:  # noqa: BLE001
        pass
    glyph.width = int(record.get("width", 0) or 0)
    if record.get("vwidth"):
        try:
            glyph.vwidth = int(record["vwidth"])
        except Exception:  # noqa: BLE001
            pass

    return glyph


def build_font(doc: dict, output: str, fmt: str) -> dict:
    """Build a font file from a document."""
    import fontforge  # noqa: PLC0415

    fmt = font_model.normalise_format(fmt)
    font = fontforge.font()

    # Metrics and names are set BEFORE any glyph work. Fonts without
    # ascent/descent silently fail to generate OTF/CFF (see engine-notes.md).
    font.em = int(doc.get("em", 1000) or 1000)
    font.ascent = int(doc.get("ascent", 800) or 0)
    font.descent = int(doc.get("descent", 200) or 0)
    for attr, key in (
        ("familyname", "family_name"),
        ("stylename", "style_name"),
        ("fontname", "font_name"),
    ):
        value = doc.get(key)
        if isinstance(value, str) and value:
            try:
                setattr(font, attr, value)
            except Exception:  # noqa: BLE001
                pass

    try:
        for record in doc.get("glyphs") or ():
            _build_glyph(font, record)

        if fmt in font_model.SAVE_FORMATS:
            font.save(output)
        else:
            font.generate(output)
    finally:
        font.close()

    if not os.path.exists(output):
        # generate() can return without writing anything; never assume success.
        raise RuntimeError(
            f"fontforge produced no output for format {fmt!r} "
            "(check ascent/descent and glyph data)"
        )

    return {"format": fmt, "bytes": os.path.getsize(output), **font_model.document_stats(doc)}


# ---------------------------------------------------------------------------
# Operations (mutate glyphs with FontForge's own tools)
# ---------------------------------------------------------------------------
#: op name -> (glyph method, positional args)
OP_SPECS: dict[str, tuple[str, tuple]] = {
    "removeOverlap": ("removeOverlap", ()),
    "addExtrema": ("addExtrema", ()),
    "simplify": ("simplify", ()),
    "correctDirection": ("correctDirection", ()),
    "round": ("round", ()),
    "autoWidth": ("autoWidth", (10, 10, 50)),
}


def op_names() -> list[str]:
    return sorted(OP_SPECS)


def apply_op(doc: dict, op_name: str, glyph_names: list[str] | None = None):
    """Run a FontForge operation over the given glyphs (or the whole font).

    Returns ``(document, applied_count, errors)``.

    The mutated font is written to a temporary native-format file and re-read
    with :func:`parse_font`, so there is exactly one serialisation path across
    upload, operations and build.
    """
    import fontforge  # noqa: PLC0415

    if op_name not in OP_SPECS:
        raise ValueError(f"unsupported operation {op_name!r}; expected one of {op_names()}")
    method, op_args = OP_SPECS[op_name]

    font = fontforge.font()
    font.em = int(doc.get("em", 1000) or 1000)
    font.ascent = int(doc.get("ascent", 800) or 0)
    font.descent = int(doc.get("descent", 200) or 0)
    for attr, key in (
        ("familyname", "family_name"),
        ("stylename", "style_name"),
        ("fontname", "font_name"),
    ):
        value = doc.get(key)
        if isinstance(value, str) and value:
            try:
                setattr(font, attr, value)
            except Exception:  # noqa: BLE001
                pass

    tmpdir = tempfile.mkdtemp(prefix="fbs-op-")
    applied = 0
    errors: list[str] = []
    try:
        for record in doc.get("glyphs") or ():
            _build_glyph(font, record)

        # Materialise the name list first: font.glyphs() is a live view and
        # mutating glyphs while iterating it is not safe.
        targets = glyph_names or [g.glyphname for g in font.glyphs()]
        for name in targets:
            try:
                glyph = font[name]
            except Exception:  # noqa: BLE001
                errors.append(f"{name}: glyph not found")
                continue
            try:
                getattr(glyph, method)(*op_args)
                applied += 1
            except Exception as exc:  # noqa: BLE001 - report and continue
                errors.append(f"{name}: {type(exc).__name__}: {exc}")

        if applied == 0 and errors:
            raise RuntimeError(f"{op_name} was not applied to any glyph: {errors[0]}")

        stamp = os.path.join(tmpdir, "result.sfd")
        font.save(stamp)
    finally:
        font.close()

    try:
        result = parse_font(stamp)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return result, applied, errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Font Builder Studio font worker")
    parser.add_argument("command", nargs="?", choices=["parse", "build", "op", "ping"])
    parser.add_argument("--input", required=False)
    parser.add_argument("--output", required=False)
    parser.add_argument("--format", default=None)
    parser.add_argument("--op", default=None, help="operation name for the 'op' command")
    parser.add_argument("--glyphs", default=None, help="comma-separated glyph names")
    parser.add_argument("--list-ops", action="store_true", help="print available operations")
    args = parser.parse_args(argv)

    if args.list_ops:
        print(json.dumps({"ok": True, "operations": op_names()}))
        return 0

    if not args.command:
        parser.error("a command is required (parse, build, op, ping)")

    if args.command == "ping":
        import fontforge  # noqa: PLC0415

        print(json.dumps({"ok": True, "fontforge": fontforge.version()}))
        return 0

    if not args.input or not args.output:
        parser.error("--input and --output are required")

    try:
        if args.command == "parse":
            doc = parse_font(args.input)
            font_model.validate_document(doc)
            with open(args.output, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            _write_result(
                f"{args.output}{RESULT_SUFFIX}",
                {"ok": True, "op": "parse", "stats": font_model.document_stats(doc)},
            )
            return 0

        with open(args.input, encoding="utf-8") as fh:
            doc = json.load(fh)
        font_model.validate_document(doc)

        if args.command == "op":
            if not args.op:
                raise ValueError("--op is required for the 'op' command")
            glyph_names = None
            if args.glyphs:
                glyph_names = [n for n in (g.strip() for g in args.glyphs.split(",")) if n]
            result, applied, errors = apply_op(doc, args.op, glyph_names)
            with open(args.output, "w", encoding="utf-8") as fh:
                json.dump(result, fh)
            _write_result(
                f"{args.output}{RESULT_SUFFIX}",
                {
                    "ok": True,
                    "op": args.op,
                    "applied": applied,
                    "errors": errors[:50],
                    "stats": font_model.document_stats(result),
                },
            )
            return 0

        fmt = args.format
        if not fmt:
            fmt = os.path.splitext(args.output)[1].lstrip(".")
        stats = build_font(doc, args.output, fmt)
        _write_result(
            f"{args.output}{RESULT_SUFFIX}", {"ok": True, "op": "build", "stats": stats}
        )
        return 0

    except BaseException as exc:  # noqa: BLE001 - must never leak a raw crash
        return _fail(args.command, args.output, exc)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
