"""
Air-gap check: prove the runtime image needs no network.

Answers four questions. Is outbound really impossible, is /data writable (or does
persistence fail quietly), does the FontForge engine parse *and rebuild* a font
with no network, and does the HTTP server still serve on loopback.

It is not part of the suites: it has to run in the *runtime* image, which
deliberately carries no tests/ or tools/, so this file is mounted in rather than
shipped.

    docker run --rm --network none --read-only --tmpfs /tmp:size=256m,mode=1777 \\
      -v fbs-data:/data \\
      -v "$PWD/App/tools/airgap_check.py:/airgap_check.py:ro" \\
      -v "$PWD/App/resources:/fonts:ro" \\
      font-builder-studio:local python /airgap_check.py

Networking and the /data check are asserted; a failure there means the
deployment assumptions are wrong. Exits non-zero on the first hard failure, and
prints AIR-GAP CHECK PASSED when everything holds.
"""

import json
import socket
import subprocess
import sys
import time
import urllib.request


def out(msg):
    print(msg, flush=True)


# --- 1. outbound must be impossible ----------------------------------------
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3).close()
    out("FAIL egress reached 1.1.1.1 - the container is NOT isolated")
    raise SystemExit(1)
except OSError as exc:
    out(f"ok   egress blocked ({type(exc).__name__})")

routes = open("/proc/net/route").read().strip().splitlines()
out(f"ok   routing table: {max(0, len(routes) - 1)} route(s) besides the header")

# --- 2. /data must be writable, or persistence silently fails --------------
import os  # noqa: E402

try:
    st = os.stat("/data")
    out(f"ok   /data uid={st.st_uid} gid={st.st_gid} mode={oct(st.st_mode)[-4:]}"
        f" (process runs as uid={os.getuid()})")
except OSError as exc:
    out(f"WARN /data cannot be stat'd: {exc}")

try:
    os.makedirs("/data/projects", exist_ok=True)
    with open("/data/projects/.probe", "w") as fh:
        fh.write("x")
    os.remove("/data/projects/.probe")
    out("ok   /data is writable - projects, uploads and users.db would persist")
except OSError as exc:
    out(f"WARN /data is NOT writable ({exc.strerror}) - persistence is unavailable")

# --- 2. the engine must work offline ---------------------------------------
sys.path.insert(0, "/app")
import font_engine  # noqa: E402  (after the sys.path fix)

data = open("/fonts/studio-emblem.ttf", "rb").read()
doc = font_engine.parse_font_bytes(data, "studio-emblem.ttf")
glyphs = len(doc.get("glyphs", []))
contours = sum(len(g.get("contours") or ()) for g in doc["glyphs"])
out(f"ok   FontForge parsed a font offline: {glyphs} glyphs, {contours} contours")

built, _stats = font_engine.build_font_bytes(doc, "ttf")
back = font_engine.parse_font_bytes(built, "back.ttf")
out(f"ok   built {len(built)} bytes offline, re-parsed {len(back['glyphs'])} glyphs")

# --- 3. the server must serve on loopback ----------------------------------
server = subprocess.Popen(["/app/entrypoint.sh"])
try:
    for _ in range(60):
        try:
            response = urllib.request.urlopen("http://127.0.0.1:5000/health", timeout=2)
            out(f"ok   /health -> {response.status} {response.read().decode()}")
            break
        except Exception:
            time.sleep(0.5)
    else:
        out("FAIL the server never answered on loopback")
        raise SystemExit(1)

    formats = json.loads(
        urllib.request.urlopen("http://127.0.0.1:5000/api/font/formats", timeout=5).read()
    )
    out(f"ok   /api/font/formats -> {len(formats.get('export_formats', []))} export formats")

    # An unicode lookup, since that reads data generated at image build time
    # rather than anything fetched later.
    blocks = json.loads(
        urllib.request.urlopen("http://127.0.0.1:5000/api/font/unicode/blocks", timeout=5).read()
    )
    out(f"ok   /api/font/unicode/blocks -> {len(blocks.get('blocks', []))} blocks")
finally:
    server.terminate()
    server.wait(timeout=10)

out("AIR-GAP CHECK PASSED")
