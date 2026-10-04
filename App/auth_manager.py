"""JWT issuing/verification, request decorators and login rate limiting.

Tokens are stateless HS256 JWTs signed with ``SECRETS`` (falling back to
``SECRET_KEY``). Logout is handled with a server-side revocation list in
``user_manager``. The whole subsystem is inert unless ``ALLOW_AUTH`` is enabled -
this module does not enforce that policy, callers do.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from functools import wraps

import jwt
from flask import g, jsonify, request

import user_manager

log = logging.getLogger("fontbuilder.auth")

JWT_SECRET = (
    os.environ.get("SECRETS", "").strip()
    or os.environ.get("SECRET_KEY", "").strip()
    or os.urandom(32).hex()
)
JWT_ALGORITHM = "HS256"
JWT_ISSUER = "font-builder-studio"
JWT_TTL_SECONDS = int(os.environ.get("JWT_TTL_SECONDS", str(24 * 3600)))

LOGIN_RATE_MAX = int(os.environ.get("LOGIN_RATE_MAX", "5"))
LOGIN_RATE_WINDOW = int(os.environ.get("LOGIN_RATE_WINDOW", "900"))
REGISTER_RATE_MAX = int(os.environ.get("REGISTER_RATE_MAX", "3"))
REGISTER_RATE_WINDOW = int(os.environ.get("REGISTER_RATE_WINDOW", "3600"))

_login_attempts: dict[str, list[float]] = {}
_register_attempts: dict[str, list[float]] = {}
_rl_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
def create_access_token(user_id, role, username, extra_claims=None):
    now = int(time.time())
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "iat": now,
        "exp": now + JWT_TTL_SECONDS,
        "iss": JWT_ISSUER,
        "jti": uuid.uuid4().hex,
    }
    if extra_claims:
        payload.update(extra_claims)
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token, payload["exp"]


def decode_token(token):
    """Claims for a valid, unexpired, non-revoked token; else None."""
    if not token or not isinstance(token, str):
        return None
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM], issuer=JWT_ISSUER)
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
    if user_manager.is_token_revoked(claims.get("jti", "")):
        return None
    return claims


def extract_token():
    """Bearer token from the Authorization header, if present."""
    auth = request.headers.get("Authorization", "") or ""
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return None


def get_current_user():
    """Claims of the authenticated user, or None."""
    return decode_token(extract_token())


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------
def optional_auth(view):
    """Attach ``g.current_user`` (may be None); never blocks."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        g.current_user = get_current_user()
        return view(*args, **kwargs)

    return wrapper


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({"ok": False, "error": "Authentication required"}), 401
        g.current_user = user
        return view(*args, **kwargs)

    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({"ok": False, "error": "Authentication required"}), 401
        if user.get("role") != "admin":
            return jsonify({"ok": False, "error": "Admin privileges required"}), 403
        g.current_user = user
        return view(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Client identity + sliding-window rate limits
# ---------------------------------------------------------------------------
def client_ip() -> str:
    if request.headers.get("X-Forwarded-For"):
        return request.headers["X-Forwarded-For"].split(",")[0].strip()
    return request.remote_addr or "-"


def login_rate_ok(ip: str) -> bool:
    now = time.time()
    with _rl_lock:
        attempts = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_RATE_WINDOW]
        _login_attempts[ip] = attempts
        return len(attempts) < LOGIN_RATE_MAX


def record_login_failure(ip: str) -> None:
    with _rl_lock:
        _login_attempts.setdefault(ip, []).append(time.time())


def reset_login_attempts(ip: str) -> None:
    with _rl_lock:
        _login_attempts.pop(ip, None)


def register_rate_ok(ip: str) -> bool:
    now = time.time()
    with _rl_lock:
        attempts = [t for t in _register_attempts.get(ip, []) if now - t < REGISTER_RATE_WINDOW]
        _register_attempts[ip] = attempts
        return len(attempts) < REGISTER_RATE_MAX


def record_register_attempt(ip: str) -> None:
    with _rl_lock:
        _register_attempts.setdefault(ip, []).append(time.time())
