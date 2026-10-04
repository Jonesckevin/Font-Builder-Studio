#!/usr/bin/env python3
"""Build the Unicode name index that powers the character picker.

Run at image build time (see ``Dockerfile``) so the runtime image ships a plain
data file and the web process never has to import FontForge.

Why an index rather than on-demand lookups? FontForge's ``UnicodeNameFromLib``
is fast (a full 0..10FFFF sweep takes well under a second) but the distro build
has no ``UnicodeData.txt`` on disk - the NamesList is compiled into the
library. Sweeping on every keystroke would be wasteful, so the result is
serialised once here as gzipped JSON (~800 KB) and loaded lazily by
``unicode_data.py``.

Output shape (parallel arrays keep the JSON - and the parse - small)::

    {
      "schema": 1,
      "nameslist": "15.0.0",
      "blocks": [["Basic Latin", 0, 127], ...],
      "codes": [32, 33, ...],            # ascending
      "names": ["SPACE", "EXCLAMATION MARK", ...],   # official names
      "glyphNames": ["space", "exclam", ...],        # FontForge short names
      "annotations": {"1234": "= snowy weather"}     # sparse, index-keyed
    }

Usage::

    python3 tools/build_unicode_index.py                 # default output
    python3 tools/build_unicode_index.py --out /tmp/x.gz
    python3 tools/build_unicode_index.py --stats         # report, write nothing
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from pathlib import Path

#: Bump when the on-disk shape changes; ``unicode_data`` refuses other values.
SCHEMA = 1

MAX_CODEPOINT = 0x110000

DEFAULT_OUT = (
    Path(__file__).resolve().parent.parent / "resources" / "unicode-index.json.gz"
)


def _import_fontforge():
    try:
        import fontforge  # noqa: PLC0415 - optional at import time
    except ImportError as exc:  # pragma: no cover - depends on the image
        raise SystemExit(
            "the fontforge python bindings are required to build the index "
            f"(install python3-fontforge): {exc}"
        ) from exc
    return fontforge


def _collect_blocks(fontforge) -> list[list]:
    """Unicode blocks, in the order FontForge returns them (ascending)."""
    return [
        [
            str(fontforge.UnicodeBlockNameFromLib(i)),
            int(fontforge.UnicodeBlockStartFromLib(i)),
            int(fontforge.UnicodeBlockEndFromLib(i)),
        ]
        for i in range(int(fontforge.UnicodeBlockCountFromLib()))
    ]


def build_index() -> dict:
    """Sweep the whole code space and return the index payload."""
    fontforge = _import_fontforge()

    blocks = _collect_blocks(fontforge)
    codes: list[int] = []
    names: list[str] = []
    glyph_names: list[str] = []
    annotations: dict[str, str] = {}

    for cp in range(MAX_CODEPOINT):
        # Empty for unassigned code points, C0/C1 controls, private use,
        # surrogates and noncharacters - which is exactly the filter we want,
        # so the picker only ever offers characters that actually exist.
        official = fontforge.UnicodeNameFromLib(cp)
        if not official:
            continue
        index = len(codes)
        codes.append(cp)
        names.append(str(official))
        glyph_names.append(str(fontforge.nameFromUnicode(cp) or ""))
        note = fontforge.UnicodeAnnotationFromLib(cp)
        if note:
            # NamesList markup ("*", "=", "x (cross-ref - 0110)") and hard
            # newlines are flattened: the text is only used for search.
            annotations[str(index)] = " ".join(str(note).split())

    try:
        nameslist = str(fontforge.UnicodeNamesListVersion()).split()[-1]
    except Exception:  # noqa: BLE001 - cosmetic only
        nameslist = "unknown"

    return {
        "schema": SCHEMA,
        "nameslist": nameslist,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(codes),
        "blocks": blocks,
        "codes": codes,
        "names": names,
        "glyphNames": glyph_names,
        "annotations": annotations,
    }


def write_index(index: dict, out: Path) -> int:
    """Write the index gzipped and reproducibly (mtime pinned). Returns bytes."""
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(index, separators=(",", ":"), ensure_ascii=False)
    blob = gzip.compress(payload.encode("utf-8"), compresslevel=9, mtime=0)
    out.write_bytes(blob)
    return len(blob)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"output path (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="report what would be built, without writing anything",
    )
    args = parser.parse_args(argv)

    started = time.time()
    index = build_index()
    elapsed = time.time() - started

    with_notes = len(index["annotations"])
    print(
        f"unicode index: {index['count']:,} named code points, "
        f"{len(index['blocks'])} blocks, {with_notes:,} annotations, "
        f"nameslist {index['nameslist']} ({elapsed:.2f}s)",
        file=sys.stderr,
    )

    if args.stats:
        return 0

    size = write_index(index, args.out)
    print(f"wrote {args.out} ({size:,} bytes gzipped)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
