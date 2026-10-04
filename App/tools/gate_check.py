#!/usr/bin/env python3
"""Font Builder Studio

Verifies that the FontForge Python bindings available in this container are
capable of everything Font Builder Studio needs. This is the *gate* for the
whole project: if it fails we fall back to building the headless wheel from
the retained source at d:\\!projects\\fontforge.

Run inside the test image:

    docker compose -f docker-compose.test.yml run --rm -T web-test \
        python tools/gate_check.py

Exit code 0 = all gates passed. Non-zero = at least one hard failure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"

# Collected results: list of (status, name, detail)
RESULTS: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    RESULTS.append((status, name, detail))
    icon = {PASS: "+", FAIL: "x", WARN: "!"}[status]
    line = f"[{icon}] {name}"
    if detail:
        line += f" - {detail}"
    print(line, flush=True)


def check(name: str, fn, *, hard: bool = True):
    """Run a single check. `hard=True` failures fail the gate."""
    try:
        detail = fn()
        record(PASS, name, str(detail) if detail else "")
        return True
    except Exception as exc:  # noqa: BLE001 - we want everything
        status = FAIL if hard else WARN
        record(status, name, f"{type(exc).__name__}: {exc}")
        if os.environ.get("GATE_VERBOSE"):
            traceback.print_exc()
        return not hard


# --------------------------------------------------------------------------
# 1. Imports
# --------------------------------------------------------------------------
def _import_fontforge():
    import fontforge  # noqa: PLC0415

    return f"fontforge {fontforge.version()}"


def _import_psmat():
    import psMat  # noqa: PLC0415

    return f"psMat ok ({type(psMat).__name__})"


def _import_both_attrs():
    import fontforge  # noqa: PLC0415
    import psMat  # noqa: PLC0415

    missing = []
    for mod, attrs in (
        (fontforge, ["open", "font", "contour", "version", "hasUserInterface"]),
        (psMat, ["identity", "scale", "translate", "rotate", "compose"]),
    ):
        for attr in attrs:
            if not hasattr(mod, attr):
                missing.append(f"{mod.__name__}.{attr}")
    if missing:
        raise AttributeError("missing: " + ", ".join(missing))
    return "all required module attributes present"


# --------------------------------------------------------------------------
# 2. Glyph / contour / point API surface
# --------------------------------------------------------------------------
def _api_surface():
    import fontforge  # noqa: PLC0415

    f = fontforge.font()
    try:
        f.em = 1000
        f.ascent = 800
        f.descent = 200
    except Exception:  # noqa: BLE001 - not all builds expose these the same way
        pass

    missing = []
    if not hasattr(f, "createChar"):
        missing.append("font.createChar")
    if not hasattr(f, "generate"):
        missing.append("font.generate")
    if not hasattr(f, "save"):
        missing.append("font.save")
    if not hasattr(f, "glyphs"):
        missing.append("font.glyphs")

    glyph = f.createChar(0x41, "A.test")
    for attr in ("foreground", "references", "width", "left_side_bearing", "export", "glyphPen"):
        if not hasattr(glyph, attr):
            missing.append(f"glyph.{attr}")

    contour = fontforge.contour()
    for attr in ("insertPoint", "closed", "is_quadratic"):
        if not hasattr(contour, attr):
            missing.append(f"contour.{attr}")

    f.close()
    if missing:
        raise AttributeError("missing: " + ", ".join(missing))
    return "font/glyph/contour/point API present"


# --------------------------------------------------------------------------
# 3. Round-trip: build a font from scratch, generate, reopen, compare
# --------------------------------------------------------------------------
def _new_font():
    """Create a font with the metrics/names fontforge needs to generate output.

    Fonts without ascent/descent silently fail to generate to some formats
    (notably OTF/CFF), so these are always set.
    """
    import fontforge  # noqa: PLC0415

    f = fontforge.font()
    for attr, value in (
        ("em", 1000),
        ("ascent", 800),
        ("descent", 200),
        ("familyname", "GateCheck"),
        ("fontname", "GateCheck-Regular"),
    ):
        try:
            setattr(f, attr, value)
        except Exception:  # noqa: BLE001 - support varies between builds
            pass
    return f


def _layer_is_quadratic(glyph) -> bool:
    """Contour order of a glyph's foreground layer.

    A new font's layer is cubic (order 3), while glyphs loaded from a TrueType
    font are quadratic (order 2). Adding a contour of the opposite order raises
    "Both arguments must be Layers of the same order", so the contour built for
    a glyph MUST match its destination layer.
    """
    try:
        return bool(glyph.foreground.is_quadratic)
    except Exception:  # noqa: BLE001
        return False  # cubic is the safe default


def _add_triangle(glyph) -> None:
    """Add a closed triangular contour, matching the layer's contour order."""
    import fontforge  # noqa: PLC0415

    contour = fontforge.contour(_layer_is_quadratic(glyph))
    contour.insertPoint((100, 0, True, fontforge.splineCorner, False))
    contour.insertPoint((500, 700, True, fontforge.splineCorner, False))
    contour.insertPoint((900, 0, True, fontforge.splineCorner, False))
    contour.closed = True

    layer = glyph.foreground
    layer += contour
    glyph.foreground = layer
    glyph.width = 1000


def _build_triangle_font(path: str, order2: bool | None = None) -> None:
    """Build a one-glyph font and generate it to `path`."""
    f = _new_font()
    glyph = f.createChar(0x41, "A")
    _add_triangle(glyph)
    f.generate(path)
    f.close()


def _roundtrip_single(fmt: str, ext: str) -> str:
    import fontforge  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, f"gate.{ext}")
        _build_triangle_font(out)
        if not os.path.exists(out):
            raise FileNotFoundError(f"{ext} not produced")

        reopened = fontforge.open(out)
        try:
            names = [g.glyphname for g in reopened.glyphs()]
            contours = 0
            for g in reopened.glyphs():
                layer = g.foreground
                contours += len(layer)
            if "A" not in names:
                raise AssertionError(f"glyph 'A' missing after {ext} round-trip: {names}")
            if contours < 1:
                raise AssertionError(f"no contours after {ext} round-trip")
        finally:
            reopened.close()
        size = os.path.getsize(out)
    return f"{ext}: 1 glyph, {contours} contour(s), {size} bytes"


def _roundtrip_ttf():
    return _roundtrip_single("truetype", "ttf")


def _roundtrip_otf():
    """OTF/CFF round-trip."""
    import fontforge  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "gate.otf")
        f = _new_font()
        glyph = f.createChar(0x42, "B")
        _add_triangle(glyph)
        f.generate(out)
        f.close()

        if not os.path.exists(out):
            raise FileNotFoundError("otf not produced")
        r = fontforge.open(out)
        try:
            if "B" not in [g.glyphname for g in r.glyphs()]:
                raise AssertionError("glyph 'B' missing after otf round-trip")
        finally:
            r.close()
        size = os.path.getsize(out)
    return f"otf: {size} bytes"


def _roundtrip_sfd():
    """SFD is FontForge's native format - required by the project."""
    import fontforge  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "gate.sfd")
        f = _new_font()
        glyph = f.createChar(0x41, "A")
        _add_triangle(glyph)
        f.save(out)
        f.close()

        if not os.path.exists(out):
            raise FileNotFoundError("sfd not produced")
        r = fontforge.open(out)
        try:
            if "A" not in [g.glyphname for g in r.glyphs()]:
                raise AssertionError("glyph 'A' missing after sfd round-trip")
        finally:
            r.close()
        size = os.path.getsize(out)
    return f"sfd: {size} bytes"


def _roundtrip_woff2():
    """WOFF2 depends on libwoff2 being compiled into the distro package."""
    import fontforge  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "gate.woff2")
        f = _new_font()
        glyph = f.createChar(0x41, "A")
        _add_triangle(glyph)
        f.generate(out)
        f.close()

        if not os.path.exists(out):
            raise FileNotFoundError("woff2 not produced")
        r = fontforge.open(out)
        try:
            if "A" not in [g.glyphname for g in r.glyphs()]:
                raise AssertionError("glyph 'A' missing after woff2 round-trip")
        finally:
            r.close()
        size = os.path.getsize(out)
    return f"woff2: {size} bytes"


def _svg_export():
    """Per-glyph SVG export, used by the browser editor's export adapter."""
    import fontforge  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        ttf = os.path.join(tmp, "seed.ttf")
        _build_triangle_font(ttf)
        f = fontforge.open(ttf)
        try:
            glyph = f["A"]
            out = os.path.join(tmp, "A.svg")
            glyph.export(out)
            if not os.path.exists(out):
                raise FileNotFoundError("svg not produced")
            head = open(out, "rb").read(200)
            if b"<svg" not in head:
                raise AssertionError("exported file is not SVG")
        finally:
            f.close()
    return "glyph.export('*.svg') works"


def _reference_glyph():
    """Component/reference glyphs (accented characters)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "gate2.ttf")
        f = _new_font()
        base = f.createChar(0x41, "A")
        _add_triangle(base)

        ref = f.createChar(0xC1, "Aacute")
        ref.addReference("A", (1, 0, 0, 1, 0, 0))
        ref.width = 1000

        f.generate(out)
        f.close()
        if not os.path.exists(out):
            raise FileNotFoundError("reference font not produced")
    return "addReference() works"


def _psmat_transform():
    """psMat-based affine transforms, used by the editor's transform tools."""
    import psMat  # noqa: PLC0415

    m = psMat.compose(psMat.scale(2.0), psMat.translate(10, 20))
    vals = tuple(m)
    if len(vals) != 6:
        raise AssertionError(f"unexpected matrix shape: {vals}")
    return f"composed 2x3 matrix {tuple(round(v, 2) for v in vals)}"


def _headless_detect():
    """The engine must report no user interface in the container."""
    import fontforge  # noqa: PLC0415

    if fontforge.hasUserInterface():
        raise AssertionError("hasUserInterface() is True - engine is not headless")
    return "headless (hasUserInterface() == False)"


def _pen_protocol():
    """glyphPen(): the preferred write path - it handles curve order itself.

    This is the API the editor uses to write outlines back, so it must work.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "pen.ttf")
        f = _new_font()
        glyph = f.createChar(0x50, "P")
        pen = glyph.glyphPen()
        pen.moveTo((100, 0))
        pen.lineTo((700, 0))
        pen.lineTo((700, 700))
        pen.closePath()
        glyph.width = 800
        f.generate(out)
        f.close()
        if not os.path.exists(out):
            raise FileNotFoundError("pen font not produced")

        import fontforge  # noqa: PLC0415

        r = fontforge.open(out)
        try:
            g = r["P"]
            n = len(g.foreground)
            if n < 1:
                raise AssertionError("pen-drawn contour missing after round-trip")
        finally:
            r.close()
    return f"glyphPen() drew {n} contour(s), round-tripped"


def _quadratic_layer_edit():
    """Editing a loaded TrueType font.

    Glyphs loaded from a TTF are quadratic, so a contour added to them must be
    quadratic too. This exercises the exact path the editor uses on import.
    """
    import fontforge  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        ttf = os.path.join(tmp, "seed.ttf")
        _build_triangle_font(ttf)
        f = fontforge.open(ttf)
        try:
            glyph = f["A"]
            is_quad = _layer_is_quadratic(glyph)
            _add_triangle(glyph)  # must not raise an order mismatch
            out = os.path.join(tmp, "edited.ttf")
            f.generate(out)
            if not os.path.exists(out):
                raise FileNotFoundError("edited ttf not produced")
        finally:
            f.close()
    return f"loaded layer quadratic={is_quad}; matching contour added, regenerated"


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
CHECKS = [
    ("import fontforge", _import_fontforge, True),
    ("import psMat", _import_psmat, True),
    ("module attribute surface", _import_both_attrs, True),
    ("headless mode", _headless_detect, True),
    ("font/glyph/contour API", _api_surface, True),
    ("round-trip: TTF", _roundtrip_ttf, True),
    ("round-trip: OTF/CFF", _roundtrip_otf, True),
    ("round-trip: SFD (native)", _roundtrip_sfd, True),
    ("round-trip: WOFF2", _roundtrip_woff2, False),
    ("edit loaded TTF (quadratic layer)", _quadratic_layer_edit, True),
    ("pen protocol (glyphPen)", _pen_protocol, True),
    ("export: per-glyph SVG", _svg_export, False),
    ("references/components", _reference_glyph, False),
    ("psMat transforms", _psmat_transform, True),
]


def _print_header() -> None:
    print("=" * 68)
    print("Font Builder Studio")
    print("=" * 68)


def _run_single(name: str) -> int:
    """Run exactly one check in this process (used for subprocess isolation)."""
    for check_name, fn, hard in CHECKS:
        if check_name == name:
            return 0 if check(check_name, fn, hard=hard) else 1
    record(FAIL, name, "unknown check")
    return 2


def _run_isolated() -> list[dict]:
    """Run every check in its own subprocess and collect the results.

    This mirrors production, where each font operation runs in a one-shot
    worker process. It also means a hard crash inside libfontforge (which is a
    real risk with this C library) is *contained* and reported as a failed
    check instead of taking down the whole run.
    """
    results: list[dict] = []
    for name, _fn, hard in CHECKS:
        try:
            proc = subprocess.run(
                [sys.executable, os.path.abspath(__file__), "--only", name],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except subprocess.TimeoutExpired:
            status = FAIL if hard else WARN
            detail = "timed out after 180s"
            results.append({"status": status, "name": name, "detail": detail})
            print(f"[{'x' if hard else '!'}] {name} - {detail}", flush=True)
            continue

        detail = ""
        for line in proc.stdout.splitlines():
            if line[:3] in ("[+]", "[x]", "[!]") and " - " in line:
                detail = line.split(" - ", 1)[1]

        if proc.returncode == 0:
            status = PASS
        else:
            status = FAIL if hard else WARN
            if proc.returncode < 0:
                detail = f"CRASHED with signal {-proc.returncode} (libfontforge segfault)"
            elif not detail:
                detail = f"exit code {proc.returncode}"
            tail = [ln for ln in (proc.stderr or "").strip().splitlines() if ln.strip()]
            if tail:
                detail += f" | {tail[-1][:160]}"

        results.append({"status": status, "name": name, "detail": detail})
        icon = {PASS: "+", FAIL: "x", WARN: "!"}[status]
        print(f"[{icon}] {name} - {detail}", flush=True)
    return results


def main(argv: list[str]) -> int:
    if "--only" in argv:
        return _run_single(argv[argv.index("--only") + 1])
    if "--list" in argv:
        for name, _, _ in CHECKS:
            print(name)
        return 0

    _print_header()

    if "--in-process" in argv:
        # Faster, but a crash inside libfontforge aborts the whole run.
        for name, fn, hard in CHECKS:
            check(name, fn, hard=hard)
        results = [{"status": s, "name": n, "detail": d} for s, n, d in RESULTS]
    else:
        results = _run_isolated()

    passed = sum(1 for r in results if r["status"] == PASS)
    failed = sum(1 for r in results if r["status"] == FAIL)
    warned = sum(1 for r in results if r["status"] == WARN)

    print("-" * 68)
    print(f"Result: {passed} passed, {failed} failed, {warned} warned")

    summary_path = os.environ.get("GATE_SUMMARY")
    if summary_path:
        with open(summary_path, "w", encoding="utf-8") as fh:
            json.dump(
                {"passed": passed, "failed": failed, "warned": warned, "results": results},
                fh,
                indent=2,
            )

    if failed:
        print("\nGATE FAILED - the engine is missing something the editor needs.")
        return 1

    print("\nGATE PASSED - the distro FontForge engine is sufficient.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
