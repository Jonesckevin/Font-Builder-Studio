"""Font Builder Studio - Flask backend.

A browser-based font editor built on FontForge's Python bindings. The engine
runs headless inside a sandboxed subprocess (see ``font_worker.py``); this
module only orchestrates HTTP.

The HTTP layer stays thin on purpose: anything that touches FontForge runs in a
sandboxed one-shot worker subprocess, never in a request handler.
"""

from __future__ import annotations

import json
import logging
import os
import secrets

from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

import auth_manager
import flags
import user_manager
from api_auth import bp as auth_api_bp
from api_font import bp as font_api_bp
from api_projects import bp as projects_api_bp
from extensions import limiter

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "config.json")

# ============================================================================
# Configuration
# ============================================================================


def _load_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


CONFIG = _load_config()
APP_META = CONFIG.get("app", {})
SEO_META = CONFIG.get("seo", {})

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "25"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# --- CORS (same-origin by default; '*' or a comma list to widen) ------------
_cors_raw = os.environ.get("CORS_ALLOWED_ORIGINS", "").strip()
if _cors_raw == "*":
    CORS(app, resources={r"/api/*": {"origins": "*"}})
elif _cors_raw:
    CORS(
        app,
        resources={r"/api/*": {"origins": [o.strip() for o in _cors_raw.split(",") if o.strip()]}},
    )

limiter.init_app(app)

if os.environ.get("TRUST_PROXY", "false").lower() == "true":
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

CONTENT_SECURITY_POLICY = os.environ.get(
    "CONTENT_SECURITY_POLICY",
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "font-src 'self' data:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'self'",
).strip()

# ============================================================================
# Logging
# ============================================================================


def _configure_logging() -> None:
    """
    Log to stdout/stderr only, which is what `docker logs` reads.

    A rotating file under /data was removed on purpose: it duplicated every line
    into the container's own state, and Docker bounds the stream already when the
    service sets logging options (see docker-compose.yml). Anything needing
    long-term retention should collect the stream, not a file in the container.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


_configure_logging()
log = logging.getLogger("fontbuilder")


# --- Feature flags ----------------------------------------------------------
# Defined in flags.py so the app and every blueprint agree on one source.
ALLOW_AUTH = flags.ALLOW_AUTH
REQUIRE_AUTH = flags.REQUIRE_AUTH
ALLOW_USER_REGISTRATION = flags.ALLOW_USER_REGISTRATION
ALLOW_GUEST_LOGIN = flags.ALLOW_GUEST_LOGIN

if ALLOW_AUTH:
    user_manager.init_db()
    log.info("Authentication enabled (require_auth=%s)", REQUIRE_AUTH)

flags.check_startup_policy()

# ============================================================================
# Blueprints
# ============================================================================

app.register_blueprint(font_api_bp)
app.register_blueprint(projects_api_bp)
app.register_blueprint(auth_api_bp)

# ============================================================================
# Security headers
# ============================================================================


@app.after_request
def _security_headers(response):
    response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
    )
    if not response.headers.get("Cache-Control") and response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store"
    if flags.TRUST_PROXY:
        # Only meaningful when a TLS-terminating proxy sits in front.
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


# ============================================================================
# Authentication gate
# ============================================================================
# The page shell, static assets and the auth endpoints must always be reachable
# so the login UI can load; every other /api/ call is gated when REQUIRE_AUTH is
# on. The client is expected to handle a 401 by showing the sign-in prompt.


def _is_public_path(path: str) -> bool:
    return path.startswith("/static/") or path.startswith("/auth/")


@app.before_request
def _enforce_auth():
    if not REQUIRE_AUTH or request.method == "OPTIONS":
        return None
    if _is_public_path(request.path):
        return None
    if not request.path.startswith("/api/"):
        return None
    if auth_manager.get_current_user():
        return None
    return jsonify({"ok": False, "error": "Authentication required"}), 401


# ============================================================================
# Routes
# ============================================================================


@app.get("/health")
def health():
    return jsonify({"status": "ok", "app": APP_META.get("title", "Font Builder Studio")})


@app.get("/")
def index():
    return render_template(
        "index.html",
        app_title=APP_META.get("title", "Font Builder Studio"),
        app_subtitle=APP_META.get("subtitle", "Browser-based font editor"),
        app_footer=APP_META.get("footer_text", APP_META.get("title", "Font Builder Studio")),
        app_repo_url=APP_META.get("repository", ""),
        app_repo_name=APP_META.get("repository_name", ""),
        seo_description=SEO_META.get("description", ""),
    )


# ============================================================================
# Error handlers
# ============================================================================


@app.errorhandler(413)
def _too_large(_err):
    return jsonify({"error": "upload too large", "max_upload_mb": MAX_UPLOAD_MB}), 413


@app.errorhandler(404)
def _not_found(_err):
    return jsonify({"error": "not found"}), 404


if __name__ == "__main__":
    # Development only. Production runs under gunicorn via entrypoint.sh.
    app.run(host="127.0.0.1", port=5000, debug=True)
