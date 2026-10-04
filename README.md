# Font Builder Studio

A browser-based font viewer/editor. Create, edit and convert fonts in the browser with an
interactive glyph-outline canvas editor. Built on **FontForge**'s Python bindings,
hosted as a single Docker container.

FileSupport|Type
-|-
|Import | SFD, SFDIR, TTF, OTF, TTC, WOFF, WOFF2, UFO/UFOZ/ZIP, PFB, PFA, SVG |
|Export | TTF, OTF, WOFF, WOFF2, SFD, SVG |

![Example](example.png)

---

## Quick start

### Docker Build

```sh
git clone https://github.com/jonesckevin/font-builder-studio.git
cd font-builder-studio
docker compose up --build
```

### Docker Hub - Run
```sh
docker run -d --name font-builder-studio -p 9017:5000 \
  --read-only --tmpfs /tmp:size=256m,mode=1777 \
  --security-opt no-new-privileges:true --cap-drop ALL \
  jonesckevin/font-builder-studio:latest
```

Open <http://localhost:9017> for the editor.

---

## Data Management

### volume and logs

State lives in a **Docker-managed named volume** (`fbs-data`), so nothing is
written into the working tree. Docker initialises a new volume from the image,
which is what makes this work: the image pre-creates `/data` owned by `appuser`,
so the volume inherits uid 1000 and is writable without any `chown` step.

To keep the files somewhere you can open instead, comment the volume line in
`docker-compose.yml` and uncomment the bind mount beneath it:

```yaml
    volumes:
      # - fbs-data:/data
      - ${DATA_DIR:-./data}:/data
```

### Backup Data File via Docker
Find, inspect or back up the volume:

```sh
docker volume ls
docker run --rm -v font-builder-studio_fbs-data:/data -v "$PWD:/out" alpine \
  tar czf /out/fbs-backup.tgz -C /data .
```

---

## Testing

### **Python**
```sh
## The Suite runs inside the test image and will test the engine, API and auth.
# Auth Test - register, login, passkey, guest, logout, session expiry, rate limits.
# API Test - parse, build, operations, projects, Unicode index.
# Engine Test - FontForge import, parse, build, cleanup operations.
docker compose -f docker-compose.test.yml run --rm -T web-test python tools/run_suites.py
```

### **NODE JS**

```sh
# Everything at once - gate, engine, API and auth - inside the test image.
# That image is the only one carrying tests/ and tools/; the production image
# has neither, so suites cannot be run against a running container any more.
# Note the separate compose file - the test service is not in docker-compose.yml.
 docker compose -f docker-compose.test.yml run --rm -T web-test

# A single suite
 docker compose -f docker-compose.test.yml run --rm -T web-test python tests/api_smoke.py

# Pure document logic (code points, spacing, point insertion, references).
# Runs on the host and needs Node, so it is deliberately not in the image.
 node App/tests/font_doc_test.mjs

# The browser font parser: reads a real TTF and checks the metrics it derives.
 node App/tests/font_parser_test.mjs

# The static build's Unicode lookup, held to the same facts as api_smoke.py.
# Needs _site (see Static build) or App/resources/unicode-index.json.
 node App/tests/unicode_static_test.mjs

# Outline geometry shared by the canvas editor and the picker's thumbnails -
# including the quadratic implied-midpoint rule, which nothing else covers.
 node App/tests/glyph_outline_test.mjs
```

---

## Licensing

FontForge is licensed **GPL-3.0-or-later**. Importing its Python bindings makes this
application a combined work with FontForge, so **this project is licensed
GPL-3.0-or-later**. The full text is in [`LICENSE`](LICENSE), unmodified.
