"""Engine smoke test - parse/build round-trips through the sandboxed worker.

Run in the test image::

    docker compose -f docker-compose.test.yml run --rm -T web-test \
        python tests/engine_smoke.py

Exits non-zero if a hard check fails.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import font_engine  # noqa: E402
import font_model  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def report(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"[{'ok' if ok else 'XX'}] {name}{' - ' + detail if detail else ''}", flush=True)


def triangle_document() -> dict:
    doc = font_model.empty_document(family_name="Smoke", style_name="Regular")
    glyph = font_model.new_glyph("A", 0x41)
    glyph["width"] = 1000
    glyph["contours"] = [
        {
            "closed": True,
            "quadratic": False,
            "points": [
                [100, 0, 1, font_model.POINT_CORNER, 0, None],
                [500, 700, 1, font_model.POINT_CORNER, 0, None],
                [900, 0, 1, font_model.POINT_CORNER, 0, None],
            ],
        }
    ]
    doc["glyphs"] = [glyph]
    return doc


def glyphs_named(doc: dict) -> dict:
    return {g["name"]: g for g in doc.get("glyphs", [])}


def main() -> int:
    print("=" * 68)
    print("Font Builder Studio - engine smoke test")
    print("=" * 68)

    # --- engine availability -------------------------------------------------
    try:
        info = font_engine.engine_info()
        if info.get("available"):
            report(True, "engine available", f"fontforge {info.get('fontforge')}")
        else:
            report(False, "engine available", str(info.get("error")))
    except Exception as exc:  # noqa: BLE001
        report(False, "engine available", f"{type(exc).__name__}: {exc}")

    doc = triangle_document()

    # --- build + parse round-trip for every format ---------------------------
    for fmt in ("ttf", "otf", "sfd", "woff2"):
        try:
            data, stats = font_engine.build_font_bytes(doc, fmt)
            back = font_engine.parse_font_bytes(data, f"roundtrip.{fmt}")
            named = glyphs_named(back)
            if "A" not in named:
                raise AssertionError(f"glyph 'A' lost: {sorted(named)}")
            contours = len(named["A"]["contours"])
            if contours < 1:
                raise AssertionError("contour lost")
            report(
                True,
                f"round-trip {fmt}",
                f"{len(data)} bytes; glyph 'A' with {contours} contour(s) preserved",
            )
        except Exception as exc:  # noqa: BLE001
            report(False, f"round-trip {fmt}", f"{type(exc).__name__}: {exc}")

    # --- hostile / corrupt input must not take anything down ------------------
    try:
        font_engine.parse_font_bytes(b"this is definitely not a font file" * 4, "garbage.ttf")
        report(False, "corrupt input rejected", "no error raised")
    except font_engine.EngineError as exc:
        report(True, "corrupt input rejected", str(exc)[:110])
    except Exception as exc:  # noqa: BLE001
        report(False, "corrupt input rejected", f"unexpected {type(exc).__name__}: {exc}")

    # --- document validation -------------------------------------------------
    try:
        bad = triangle_document()
        bad["glyphs"][0]["unicode"] = 999_999_999  # out of range
        font_engine.build_font_bytes(bad, "ttf")
        report(False, "invalid document rejected", "no error raised")
    except font_model.DocumentError as exc:
        report(True, "invalid document rejected", str(exc)[:110])
    except Exception as exc:  # noqa: BLE001
        report(False, "invalid document rejected", f"unexpected {type(exc).__name__}: {exc}")

    # --- soft: references ----------------------------------------------------
    try:
        ref_doc = triangle_document()
        base = ref_doc["glyphs"][0]
        accented = font_model.new_glyph("Aacute", 0xC1)
        accented["width"] = 1000
        accented["references"] = [["A", [1, 0, 0, 1, 0, 0], False]]
        ref_doc["glyphs"] = [base, accented]

        data, _ = font_engine.build_font_bytes(ref_doc, "ttf")
        back = font_engine.parse_font_bytes(data, "refs.ttf")
        named = glyphs_named(back)
        if "Aacute" not in named:
            raise AssertionError("accented glyph missing")
        refs = named["Aacute"]["references"]
        if not refs:
            raise AssertionError("reference not preserved")
        report(True, "references", f"{len(refs)} reference(s) preserved")
    except Exception as exc:  # noqa: BLE001
        report(False, "references", f"{type(exc).__name__}: {exc}")

    # --- soft: unencoded glyph ----------------------------------------------
    try:
        un_doc = triangle_document()
        alt = font_model.new_glyph("A.alt", -1)
        alt["width"] = 900
        alt["contours"] = triangle_document()["glyphs"][0]["contours"]
        un_doc["glyphs"] = [un_doc["glyphs"][0], alt]

        data, _ = font_engine.build_font_bytes(un_doc, "ttf")
        back = font_engine.parse_font_bytes(data, "unenc.ttf")
        named = glyphs_named(back)
        if "A.alt" not in named:
            raise AssertionError(f"unencoded glyph missing: {sorted(named)}")
        report(True, "unencoded glyph", "A.alt preserved with unicode -1")
    except Exception as exc:  # noqa: BLE001
        report(False, "unencoded glyph", f"{type(exc).__name__}: {exc}")

    failures = sum(1 for passed, _name, _detail in RESULTS if not passed)
    print("-" * 68)
    print(f"{len(RESULTS) - failures}/{len(RESULTS)} checks passed")
    if failures:
        print("\nSMOKE TEST FAILED")
        return 1
    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
