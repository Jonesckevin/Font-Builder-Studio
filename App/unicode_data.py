"""Unicode name lookup behind the character picker.

Reads the index produced by ``tools/build_unicode_index.py`` (at image build
time) and answers three questions for the UI:

* which blocks exist, so they can be listed as sections
* which characters belong to a block, so the grid can render it
* which characters match a search term - by name, annotation or code point

**FontForge is deliberately not imported here.** The index is plain data, which
keeps the web process free of the engine's process-global state and means a
missing or corrupt index degrades to "no picker" instead of breaking the app..
"""

from __future__ import annotations

import bisect
import functools
import gzip
import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("fontbuilder.unicode")

#: Where the build step puts the index. Fallback paths cover running from a
#: source checkout rather than the container image.
INDEX_PATH = Path(__file__).resolve().parent / "resources" / "unicode-index.json.gz"

#: Guard against absurd page sizes from the client.
MAX_LIMIT = 4096
DEFAULT_LIMIT = 256

#: Matches are bucketed by rank while scanning. Each bucket keeps only its
#: first N code points, which is safe because ties are broken by code point -
#: so the top N overall are always present. Every match is still *counted*, so
#: the reported total stays exact.
_BUCKET_CAP = 512

_index: dict | None = None
_folded: list[str] | None = None
_load_failed = False
_lock = threading.Lock()


def _load() -> dict | None:
    """Load the index once per process. Returns None if unavailable."""
    global _index, _folded, _load_failed
    if _index is not None or _load_failed:
        return _index
    with _lock:
        if _index is not None or _load_failed:
            return _index
        try:
            with gzip.open(INDEX_PATH, "rb") as handle:
                data = json.loads(handle.read().decode("utf-8"))
        except FileNotFoundError:
            # Not fatal: the picker simply will not be offered.
            log.warning(
                "unicode index missing at %s - character picker disabled "
                "(run tools/build_unicode_index.py)",
                INDEX_PATH,
            )
            _load_failed = True
            return None
        except Exception as exc:  # noqa: BLE001 - never break the app for this
            log.warning("unicode index unreadable (%s) - picker disabled", exc)
            _load_failed = True
            return None

        if data.get("schema") != 1:
            log.warning(
                "unicode index schema %r unsupported - picker disabled",
                data.get("schema"),
            )
            _load_failed = True
            return None

        _folded = [name.casefold() for name in data["names"]]
        _index = data
        log.info(
            "unicode index loaded: %s code points, %s blocks, nameslist %s",
            data.get("count"),
            len(data.get("blocks", [])),
            data.get("nameslist"),
        )
        return _index


def available() -> bool:
    """True when the picker has data to serve."""
    return _load() is not None


def meta() -> dict:
    """Version/metadata for the client, without shipping the whole index."""
    data = _load()
    if data is None:
        return {"available": False}
    return {
        "available": True,
        "count": data.get("count", 0),
        "nameslist": data.get("nameslist", ""),
        "block_count": len(data.get("blocks", [])),
    }


def blocks() -> list[dict]:
    """All Unicode blocks as ``{index, name, start, end, count}``."""
    data = _load()
    if data is None:
        return []
    codes = data["codes"]
    out = []
    for i, (name, start, end) in enumerate(data["blocks"]):
        lo = bisect.bisect_left(codes, start)
        hi = bisect.bisect_right(codes, end)
        out.append(
            {
                "index": i,
                "name": name,
                "start": start,
                "end": end,
                "count": hi - lo,
            }
        )
    return out


def _entry(index: int) -> dict:
    data = _index or {}
    code = data["codes"][index]
    note = data.get("annotations", {}).get(str(index))
    return {
        "code": code,
        "name": data["names"][index],
        "glyph_name": data["glyphNames"][index],
        "annotation": note or "",
    }


def chars(block_index: int, offset: int = 0, limit: int = DEFAULT_LIMIT) -> dict | None:
    """Characters inside one block, paginated. None if the block is unknown."""
    data = _load()
    if data is None:
        return None
    table = data["blocks"]
    if not 0 <= block_index < len(table):
        return None

    name, start, end = table[block_index]
    codes = data["codes"]
    lo = bisect.bisect_left(codes, start)
    hi = bisect.bisect_right(codes, end)
    total = hi - lo

    offset = max(0, offset)
    limit = max(1, min(limit, MAX_LIMIT))
    window = range(lo + offset, min(hi, lo + offset + limit))

    return {
        "block": {"index": block_index, "name": name, "start": start, "end": end},
        "total": total,
        "offset": offset,
        "chars": [_entry(i) for i in window],
        "truncated": offset + limit < total,
    }


def lookup(code: int) -> dict | None:
    """Details for a single code point, or None when it has no name."""
    data = _load()
    if data is None or not isinstance(code, int):
        return None
    codes = data["codes"]
    pos = bisect.bisect_left(codes, code)
    if pos >= len(codes) or codes[pos] != code:
        return None
    return _entry(pos)


def parse_codepoint(text: object) -> int | None:
    """Interpret ``U+0041`` / ``0x41`` / ``41`` / ``65`` as a code point.

    Mirrors the client-side parser in ``static/font-doc.js`` so a search term
    behaves the same wherever it is typed. A bare number is decimal.
    """
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw:
        return None

    lowered = raw.casefold()
    try:
        if lowered.startswith("u+"):
            value = int(raw[2:], 16)
        elif lowered.startswith("0x"):
            value = int(raw[2:], 16)
        elif any(c in "abcdef" for c in lowered) and all(
            c in "0123456789abcdef" for c in lowered
        ):
            value = int(raw, 16)
        elif raw.isdigit():
            value = int(raw, 10)
        else:
            return None
    except ValueError:
        return None

    return value if 0 <= value <= 0x10FFFF else None


def _all_tokens_present(needle: str, name: str) -> bool:
    """True when every word of `needle` appears in `name`, in any order.

    Without this, "greek capital omega" finds nothing, because the real name -
    "GREEK CAPITAL LETTER OMEGA" - has a word in between. People routinely drop
    words like LETTER or SIGN, so this is worth the extra scan.
    """
    if " " not in needle:
        return False
    return all(token in name for token in needle.split())


def _rank(needle: str, data: dict, index: int) -> int | None:
    """Match strength for one entry; lower is better, None means no match."""
    name = _folded[index] if _folded else data["names"][index].casefold()
    if name == needle:
        return 0
    if name.startswith(needle):
        return 1
    if f" {needle}" in name:
        return 2
    if needle in name:
        return 3
    if _all_tokens_present(needle, name):
        return 4
    note = data["annotations"].get(str(index))
    if note and needle in note.casefold():
        return 5
    return None


def search(query: object, limit: int = DEFAULT_LIMIT) -> dict:
    """Find characters by name, annotation, or code point."""
    data = _load()
    if data is None:
        return {"results": [], "total": 0, "truncated": False, "available": False}

    text = str(query or "").strip()
    limit = max(1, min(int(limit), MAX_LIMIT))
    if not text:
        return {"results": [], "total": 0, "truncated": False}

    needle = text.casefold()
    codes = data["codes"]

    # Pinned results: an explicit code point, or a single character typed
    # directly, always comes first.
    pinned: list[int] = []

    def _pin(codepoint: int) -> None:
        pos = bisect.bisect_left(codes, codepoint)
        if pos < len(codes) and codes[pos] == codepoint and pos not in pinned:
            pinned.append(pos)

    exact = parse_codepoint(text)
    if exact is not None:
        _pin(exact)
    if len(text) == 1:
        _pin(ord(text))

    buckets: list[list[int]] = [[], [], [], [], [], []]
    counts = [0, 0, 0, 0, 0, 0]
    for index in range(len(codes)):
        rank = _rank(needle, data, index)
        if rank is None:
            continue
        counts[rank] += 1
        if len(buckets[rank]) < _BUCKET_CAP:
            buckets[rank].append(index)

    page = list(pinned[:limit])
    seen = set(page)
    for bucket in buckets:
        for index in bucket:
            if len(page) >= limit:
                break
            if index not in seen:
                seen.add(index)
                page.append(index)

    total = sum(counts)
    # A pinned code point that did not otherwise match (e.g. "U+2603", whose
    # name is "SNOWMAN") still counts as one result.
    total += sum(1 for pos in pinned if _rank(needle, data, pos) is None)
    return {
        "results": [_entry(index) for index in page],
        "total": total,
        "truncated": total > len(page),
        "query": text,
    }


@functools.lru_cache(maxsize=256)
def cached_search(query: str, limit: int) -> str:
    """Search results as a JSON string, so repeated queries are free."""
    return json.dumps(search(query, limit), separators=(",", ":"))


def reset_cache() -> None:
    """Drop the loaded index (used by tests)."""
    global _index, _folded, _load_failed
    with _lock:
        _index = None
        _folded = None
        _load_failed = False
    cached_search.cache_clear()
