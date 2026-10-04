"""HTTP API smoke test - exercises the font endpoints end to end.

Runs *inside* the test image, against the server the suite runner starts::

    docker compose -f docker-compose.test.yml run --rm -T web-test \
        python tests/api_smoke.py

Uses only the standard library, so no extra dependencies in the image.
Exits non-zero on failure.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = os.environ.get("FBS_BASE_URL", "http://127.0.0.1:5000")

RESULTS: list[tuple[bool, str, str]] = []


def report(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"[{'ok' if ok else 'XX'}] {name}{' - ' + detail if detail else ''}", flush=True)


def _request(req: urllib.request.Request):
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def get(path: str):
    return _request(urllib.request.Request(f"{BASE}{path}", method="GET"))


def post_json(path: str, payload: dict):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return _request(req)


def post_file(path: str, filename: str, data: bytes):
    boundary = f"----fbs{uuid.uuid4().hex}"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    return _request(req)


def json_body(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def put_json(path: str, payload: dict):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    return _request(req)


def delete(path: str):
    return _request(urllib.request.Request(f"{BASE}{path}", method="DELETE"))


def main() -> int:
    print("=" * 68)
    print(f"Font Builder Studio - API smoke test ({BASE})")
    print("=" * 68)

    # --- health -------------------------------------------------------------
    status, _, raw = get("/health")
    report(status == 200 and json_body(raw).get("status") == "ok", "GET /health", f"HTTP {status}")

    # --- formats ------------------------------------------------------------
    status, _, raw = get("/api/font/formats")
    formats = json_body(raw)
    names = [f["format"] for f in formats.get("export_formats", [])]
    report(
        status == 200 and "ttf" in names and "sfd" in names and "sfdir" not in names,
        "GET /api/font/formats",
        f"exports={names}",
    )

    ops = formats.get("operations") or []
    report("removeOverlap" in ops, "formats exposes operations", f"ops={ops}")

    # --- new document -------------------------------------------------------
    status, _, raw = post_json(
        "/api/font/new", {"family_name": "Smoke Test", "style_name": "Regular"}
    )
    new_body = json_body(raw)
    doc = new_body.get("document") or {}
    report(
        status == 200 and doc.get("family_name") == "Smoke Test" and doc.get("glyphs") == [],
        "POST /api/font/new",
        f"HTTP {status}, em={doc.get('em')}",
    )

    # --- populate a glyph and build each format -----------------------------
    doc["glyphs"] = [
        {
            "name": "A",
            "unicode": 0x41,
            "width": 1000,
            "lsb": 0,
            "vwidth": 0,
            "quadratic": False,
            "contours": [
                {
                    "closed": True,
                    "quadratic": False,
                    "points": [
                        [100, 0, 1, 0, 0, None],
                        [500, 700, 1, 0, 0, None],
                        [900, 0, 1, 0, 0, None],
                    ],
                }
            ],
            "references": [],
        }
    ]

    built: dict[str, bytes] = {}
    for fmt in ("ttf", "otf", "sfd", "woff2"):
        status, headers, raw = post_json(
            "/api/font/build", {"document": doc, "format": fmt, "filename": f"smoke.{fmt}"}
        )
        ok = status == 200 and len(raw) > 100 and "X-Font-Stats" in headers
        if ok:
            built[fmt] = raw
        report(ok, f"POST /api/font/build ({fmt})", f"HTTP {status}, {len(raw)} bytes")

    # --- parse the built TTF back -------------------------------------------
    if "ttf" in built:
        status, _, raw = post_file("/api/font/parse", "smoke.ttf", built["ttf"])
        parsed = json_body(raw)
        glyphs = {g["name"]: g for g in parsed.get("document", {}).get("glyphs", [])}
        ok = status == 200 and "A" in glyphs and len(glyphs["A"]["contours"]) >= 1
        report(ok, "POST /api/font/parse (round-trip)", f"HTTP {status}, glyphs={sorted(glyphs)}")

        # --- rebuild from the parsed document (full server round-trip) -------
        if ok:
            status, _, raw = post_json(
                "/api/font/build", {"document": parsed["document"], "format": "otf"}
            )
            detail = f"HTTP {status}, {len(raw)} bytes"
            if status != 200:
                detail = f"HTTP {status}: {json_body(raw).get('error', '')[:150]}"
            report(status == 200 and len(raw) > 100, "rebuild from parsed document", detail)

    # --- stats_only mode ----------------------------------------------------
    if "ttf" in built:
        status, _, raw = post_file("/api/font/parse?stats_only=1", "smoke.ttf", built["ttf"])
        body = json_body(raw)
        report(
            status == 200 and "document" not in body and body.get("stats", {}).get("glyphs"),
            "parse stats_only omits document",
            f"stats={body.get('stats')}",
        )

    # --- error handling -----------------------------------------------------
    status, _, raw = post_file("/api/font/parse", "evil.exe", b"MZ\x90\x00" + b"\x00" * 64)
    report(status == 415, "rejects unsupported upload type", f"HTTP {status}")

    status, _, raw = post_file("/api/font/parse", "empty.ttf", b"")
    report(status == 400, "rejects empty upload", f"HTTP {status}")

    status, _, raw = post_file("/api/font/parse", "garbage.ttf", b"not a font" * 20)
    report(status == 422, "rejects unparsable font", f"HTTP {status}")

    status, _, raw = post_json("/api/font/build", {"document": doc, "format": "nope"})
    report(status == 400, "rejects unknown format", f"HTTP {status}")

    status, _, raw = post_json("/api/font/build", {"format": "ttf"})
    report(status == 400, "rejects missing document", f"HTTP {status}")

    status, _, raw = post_json("/api/font/build", {"document": doc, "format": "sfdir"})
    report(status == 400, "rejects sfdir download", f"HTTP {status}")

    bad = json.loads(json.dumps(doc))
    bad["glyphs"][0]["unicode"] = 99_999_999
    status, _, raw = post_json("/api/font/build", {"document": bad, "format": "ttf"})
    report(status == 400, "rejects invalid document", f"HTTP {status}")

    # --- projects -----------------------------------------------------------
    project_id = None
    status, _, raw = post_json("/api/projects", {"name": "Smoke Project", "document": doc})
    created = (json_body(raw).get("project") or {})
    project_id = created.get("id")
    report(
        status == 201 and bool(project_id),
        "POST /api/projects",
        f"HTTP {status}, id={project_id}",
    )

    if project_id:
        status, _, raw = get(f"/api/projects/{project_id}")
        loaded_doc = ((json_body(raw).get("project") or {}).get("document")) or {}
        report(
            status == 200 and len(loaded_doc.get("glyphs") or []) == 1,
            "GET /api/projects/<id>",
            f"HTTP {status}, glyphs={len(loaded_doc.get('glyphs') or [])}",
        )

        status, _, raw = put_json(
            f"/api/projects/{project_id}",
            {"name": "Renamed Project", "document": doc},
        )
        report(
            status == 200 and (json_body(raw).get("project") or {}).get("name") == "Renamed Project",
            "PUT /api/projects/<id>",
            f"HTTP {status}",
        )

        status, _, raw = get("/api/projects")
        listing = json_body(raw).get("projects") or []
        report(
            any(p.get("id") == project_id for p in listing),
            "GET /api/projects lists it",
            f"{len(listing)} project(s)",
        )

        status, _, raw = get(f"/api/projects/{project_id}")
        report(status == 200, "GET /api/projects/<id> round-trip", f"HTTP {status}")

    # Path traversal must be refused outright, not resolved.
    status, _, raw = get("/api/projects/..%2F..%2Fetc%2Fpasswd")
    report(status in (400, 404), "rejects path-traversal project id", f"HTTP {status}")

    status, _, raw = get("/api/projects/00000000000000000000000000000000")
    report(status == 404, "missing project is 404", f"HTTP {status}")

    # --- operations ---------------------------------------------------------
    status, _, raw = post_json("/api/font/op", {"document": doc, "op": "removeOverlap"})
    body = json_body(raw)
    report(
        status == 200 and isinstance(body.get("document"), dict),
        "POST /api/font/op (removeOverlap)",
        f"HTTP {status}, applied={body.get('applied')}",
    )

    status, _, raw = post_json(
        "/api/font/op", {"document": doc, "op": "addExtrema", "glyphs": ["A"]}
    )
    body = json_body(raw)
    report(
        status == 200 and body.get("applied") == 1,
        "POST /api/font/op (scoped to one glyph)",
        f"HTTP {status}, applied={body.get('applied')}",
    )

    status, _, raw = post_json("/api/font/op", {"document": doc, "op": "notARealOp"})
    report(status == 400, "rejects unknown operation", f"HTTP {status}")

    status, _, raw = post_json("/api/font/op", {"document": doc})
    report(status == 400, "rejects missing operation", f"HTTP {status}")

    # --- unicode picker -----------------------------------------------------
    def u(path: str):
        return get(f"/api/font/unicode/{path}")

    def q(text: str, limit: int = 8) -> str:
        return f"search?q={urllib.parse.quote(text)}&limit={limit}"

    status, _, raw = get("/api/font/formats")
    picker = json_body(raw).get("unicode_picker") or {}
    report(
        status == 200 and picker.get("available"),
        "/formats advertises the character picker",
        f"{picker.get('count')} code points, nameslist {picker.get('nameslist')}",
    )

    status, _, raw = u("blocks")
    body = json_body(raw)
    blocks = body.get("blocks") or []
    latin = next((b for b in blocks if b.get("name") == "Basic Latin"), None)
    report(
        status == 200 and len(blocks) > 300 and latin is not None,
        "GET /api/font/unicode/blocks",
        f"{len(blocks)} blocks",
    )

    if latin:
        status, _, raw = u(f"chars?block={latin['index']}&limit=5")
        body = json_body(raw)
        chars = body.get("chars") or []
        report(
            status == 200
            and len(chars) == 5
            and chars[0].get("code") == 32
            and body.get("total") == latin["count"]
            and body.get("truncated") is True,
            "GET /api/font/unicode/chars (paginates a block)",
            f"first={chars[0].get('name') if chars else None}, total={body.get('total')}",
        )

        status, _, raw = u(
            f"chars?block={latin['index']}&offset={latin['count'] - 2}&limit=10"
        )
        body = json_body(raw)
        report(
            status == 200 and body.get("truncated") is False,
            "chars pagination ends cleanly",
            f"offset={body.get('offset')}, got={len(body.get('chars') or [])}",
        )

    status, _, raw = u("chars?block=99999")
    report(status == 404, "chars rejects an unknown block", f"HTTP {status}")

    status, _, raw = u("search?q=")
    report(status == 200, "search tolerates an empty query", f"HTTP {status}")

    status, _, raw = u(q("snowman"))
    body = json_body(raw)
    first = (body.get("results") or [{}])[0]
    report(
        status == 200 and first.get("code") == 0x2603 and first.get("name") == "SNOWMAN",
        "search by name",
        f"{body.get('total')} hits, first={first.get('name')}",
    )

    # The same character must be findable in every notation the field accepts.
    for notation, label in (("U+2603", "U+ form"), ("0x2603", "0x form"), ("9731", "decimal")):
        status, _, raw = u(q(notation))
        body = json_body(raw)
        codes = [r.get("code") for r in (body.get("results") or [])]
        report(
            status == 200 and 0x2603 in codes,
            f"search by code point ({label})",
            f"{notation} -> {codes[:3]}",
        )

    # People drop words like LETTER/SIGN, so token matching has to cover it.
    status, _, raw = u(q("greek capital omega"))
    body = json_body(raw)
    codes = [r.get("code") for r in (body.get("results") or [])]
    report(
        status == 200 and 0x03A9 in codes,
        "search matches when words are skipped",
        f"'greek capital omega' -> {codes[:3]}",
    )

    status, _, raw = u(q("zzzznope"))
    body = json_body(raw)
    report(
        status == 200 and body.get("total") == 0 and not body.get("results"),
        "search returns nothing for nonsense",
        f"HTTP {status}",
    )

    status, _, raw = u("lookup?code=U%2B2603")
    body = json_body(raw)
    report(
        status == 200
        and body.get("name") == "SNOWMAN"
        and body.get("glyph_name") == "uni2603"
        and body.get("named") is True,
        "GET /api/font/unicode/lookup (U+ form)",
        f"{body.get('name')} / {body.get('glyph_name')}",
    )

    status, _, raw = u("lookup?code=0xE9")
    body = json_body(raw)
    report(
        status == 200 and body.get("name") == "LATIN SMALL LETTER E WITH ACUTE",
        "lookup accepts 0x notation",
        f"{body.get('name')}",
    )

    # Unassigned code points are legal to encode, they simply have no name.
    status, _, raw = u("lookup?code=888")
    body = json_body(raw)
    report(
        status == 200 and body.get("named") is False and body.get("code") == 0x0378,
        "lookup reports unnamed code points without failing",
        f"HTTP {status}, named={body.get('named')}",
    )

    status, _, raw = u("lookup")
    report(status == 400, "lookup requires a code parameter", f"HTTP {status}")

    # Clean up the project created above.
    if project_id:
        status, _, raw = delete(f"/api/projects/{project_id}")
        report(status == 200, "DELETE /api/projects/<id>", f"HTTP {status}")
        status, _, raw = get(f"/api/projects/{project_id}")
        report(status == 404, "deleted project is gone", f"HTTP {status}")

    failures = sum(1 for ok, _n, _d in RESULTS if not ok)
    print("-" * 68)
    print(f"{len(RESULTS) - failures}/{len(RESULTS)} checks passed")
    if failures:
        print("\nAPI SMOKE TEST FAILED")
        return 1
    print("\nAPI SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
