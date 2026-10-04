"""SQLite-backed accounts, password hashing and token revocation.

Storage is a single SQLite database under ``/data`` so it persists on the same
volume as projects. Passwords use bcrypt. Every operation opens a short-lived
connection (WAL mode), which is plenty for this app's low-concurrency auth
workload.

This module only manages storage - it never enforces policy. ``api_auth.py``
decides who may do what, and everything is inert unless ``ALLOW_AUTH`` is on.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

import bcrypt

log = logging.getLogger("fontbuilder.users")

USERS_DB = os.environ.get("USERS_DB", "/data/users.db")
GUEST_TTL_HOURS = int(os.environ.get("GUEST_TTL_HOURS", "24"))

_write_lock = threading.Lock()

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")

VALID_ROLES = ("user", "admin", "guest")
VALID_STATUSES = ("active", "banned")

#: Hash prefix marking a deliberately unusable password (guest / passkey-only).
UNUSABLE_PREFIX = "!!unusable-"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _connect() -> sqlite3.Connection:
    directory = os.path.dirname(USERS_DB)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(USERS_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    """Create tables if absent. Safe to call repeatedly."""
    with _write_lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id       TEXT PRIMARY KEY,
                username      TEXT UNIQUE NOT NULL COLLATE NOCASE,
                email         TEXT,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'user',
                status        TEXT NOT NULL DEFAULT 'active',
                is_guest      INTEGER NOT NULL DEFAULT 0,
                expires_at    INTEGER,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS revoked_tokens (
                jti        TEXT PRIMARY KEY,
                expires_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id       TEXT PRIMARY KEY,
                settings_json TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS passkeys (
                cred_id      TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                public_key   TEXT NOT NULL,
                sign_count   INTEGER NOT NULL DEFAULT 0,
                transports   TEXT NOT NULL DEFAULT '[]',
                created_at   TEXT NOT NULL,
                last_used_at TEXT
            )
            """
        )
        conn.commit()
    log.info("User database ready at %s", USERS_DB)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_username(username) -> str | None:
    if not isinstance(username, str) or not _USERNAME_RE.match(username or ""):
        return "Username must be 3-32 characters: letters, numbers, dot, underscore or hyphen"
    return None


def validate_password(password) -> str | None:
    if not isinstance(password, str) or len(password) < 12:
        return "Password must be at least 12 characters"
    if len(password) > 128:
        return "Password must be at most 128 characters"
    if not re.search(r"[a-z]", password):
        return "Password must include a lowercase letter"
    if not re.search(r"[A-Z]", password):
        return "Password must include an uppercase letter"
    if not re.search(r"[0-9]", password):
        return "Password must include a number"
    return None


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash or password_hash.startswith(UNUSABLE_PREFIX):
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _public(row) -> dict | None:
    if row is None:
        return None
    keys = row.keys()
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "email": row["email"],
        "role": row["role"],
        "status": row["status"],
        "is_guest": bool(row["is_guest"]) if "is_guest" in keys else False,
        "expires_at": row["expires_at"] if "expires_at" in keys else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def count_users() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def get_user_by_username(username):
    if not username:
        return None
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (str(username).strip(),)
        ).fetchone()


def get_user_by_id(user_id):
    if not user_id:
        return None
    with _connect() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def _insert_user(user_id, username, email, password_hash, role, is_guest=0, expires_at=None):
    now = _now()
    with _write_lock, _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO users (user_id, username, email, password_hash, role, status,"
                " is_guest, expires_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (user_id, username, email, password_hash, role, "active", is_guest, expires_at, now, now),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError("Username already exists") from None
    return {
        "user_id": user_id,
        "username": username,
        "email": email,
        "role": role,
        "status": "active",
        "is_guest": bool(is_guest),
        "expires_at": expires_at,
        "created_at": now,
        "updated_at": now,
    }


def create_user(username, password, email=None, role="user") -> dict:
    """Create a password account. Raises ValueError on validation/duplicate."""
    username = (username or "").strip()
    err = validate_username(username)
    if err:
        raise ValueError(err)
    err = validate_password(password)
    if err:
        raise ValueError(err)
    if role not in VALID_ROLES:
        role = "user"
    user = _insert_user(
        str(uuid.uuid4()), username, (email or "").strip() or None,
        hash_password(password), role,
    )
    log.info("Created user %r (role=%s)", username, role)
    return user


def create_user_passwordless(username, email=None, role="user", forced_id=None) -> dict:
    """Create a user with an unusable password hash (passkey-only account).

    The hash starts with the reserved ``!!unusable-`` prefix, which
    :func:`verify_password` rejects before any bcrypt work happens.
    """
    username = (username or "").strip()
    err = validate_username(username)
    if err:
        raise ValueError(err)
    if role not in VALID_ROLES:
        role = "user"
    user = _insert_user(
        forced_id or str(uuid.uuid4()), username, (email or "").strip() or None,
        UNUSABLE_PREFIX + uuid.uuid4().hex, role,
    )
    log.info("Created passwordless user %r (role=%s)", username, role)
    return user


def authenticate(username, password) -> dict | None:
    """Public user dict on success, else None. Hardened against user probing."""
    row = get_user_by_username(username)
    if row is None:
        # Spend a comparable amount of time so a missing user is not faster.
        hash_password("timing~guard~000000")
        return None
    if row["status"] != "active":
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return _public(row)


def create_guest_user() -> dict:
    """Create a short-lived guest account."""
    expires_ts = int(time.time()) + GUEST_TTL_HOURS * 3600
    user = _insert_user(
        str(uuid.uuid4()), "Guest_" + uuid.uuid4().hex[:8], None,
        UNUSABLE_PREFIX + uuid.uuid4().hex, "guest", is_guest=1, expires_at=expires_ts,
    )
    log.info("Created guest %s (expires %s)", user["username"], expires_ts)
    return user


def purge_expired_guests() -> int:
    """Delete guests whose TTL has elapsed, plus their settings."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id FROM users WHERE is_guest = 1 AND expires_at IS NOT NULL"
            " AND expires_at < ?",
            (int(time.time()),),
        ).fetchall()
    ids = [r["user_id"] for r in rows]
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    with _write_lock, _connect() as conn:
        conn.execute(f"DELETE FROM user_settings WHERE user_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM users WHERE user_id IN ({placeholders})", ids)
        conn.commit()
    log.info("Purged %d expired guest account(s)", len(ids))
    return len(ids)


def list_users(limit=50, offset=0) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
    return [_public(r) for r in rows]


def set_status(user_id, status) -> bool:
    if status not in VALID_STATUSES:
        raise ValueError("Invalid status")
    with _write_lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE users SET status = ?, updated_at = ? WHERE user_id = ?",
            (status, _now(), user_id),
        )
        conn.commit()
    return cur.rowcount > 0


def delete_user(user_id) -> bool:
    with _write_lock, _connect() as conn:
        conn.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
        cur = conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Token revocation
# ---------------------------------------------------------------------------
def revoke_token(jti, expires_at) -> None:
    if not jti:
        return
    with _write_lock, _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO revoked_tokens (jti, expires_at) VALUES (?, ?)",
            (jti, int(expires_at or 0)),
        )
        conn.commit()


def is_token_revoked(jti) -> bool:
    if not jti:
        return False
    with _connect() as conn:
        return (
            conn.execute("SELECT 1 FROM revoked_tokens WHERE jti = ?", (jti,)).fetchone()
            is not None
        )


def cleanup_expired_tokens() -> int:
    """Drop revocation entries whose tokens have already expired."""
    with _write_lock, _connect() as conn:
        cur = conn.execute("DELETE FROM revoked_tokens WHERE expires_at < ?", (int(time.time()),))
        conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Per-user settings (used for encrypted secrets at rest)
# ---------------------------------------------------------------------------
def get_user_settings(user_id):
    if not user_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT settings_json FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["settings_json"])
    except (ValueError, TypeError):
        return None


def save_user_settings(user_id, settings) -> bool:
    if not user_id:
        return False
    with _write_lock, _connect() as conn:
        conn.execute(
            "INSERT INTO user_settings (user_id, settings_json, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET settings_json = excluded.settings_json,"
            " updated_at = excluded.updated_at",
            (user_id, json.dumps(settings), _now()),
        )
        conn.commit()
    return True


# ---------------------------------------------------------------------------
# Passkeys (WebAuthn credentials)
# ---------------------------------------------------------------------------
def add_passkey(user_id, cred_id, public_key_b64url, sign_count, transports) -> None:
    with _write_lock, _connect() as conn:
        conn.execute(
            "INSERT INTO passkeys (cred_id, user_id, public_key, sign_count, transports, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (cred_id, user_id, public_key_b64url, int(sign_count), json.dumps(list(transports)), _now()),
        )
        conn.commit()


def get_passkey(cred_id):
    if not cred_id:
        return None
    with _connect() as conn:
        row = conn.execute("SELECT * FROM passkeys WHERE cred_id = ?", (cred_id,)).fetchone()
    if not row:
        return None
    return {
        "cred_id": row["cred_id"],
        "user_id": row["user_id"],
        "public_key": row["public_key"],
        "sign_count": row["sign_count"],
        "transports": json.loads(row["transports"] or "[]"),
    }


def update_passkey_sign_count(cred_id, new_count) -> None:
    with _write_lock, _connect() as conn:
        conn.execute(
            "UPDATE passkeys SET sign_count = ?, last_used_at = ? WHERE cred_id = ?",
            (int(new_count), _now(), cred_id),
        )
        conn.commit()


def list_user_passkeys(user_id) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT cred_id, created_at, last_used_at, transports FROM passkeys"
            " WHERE user_id = ? ORDER BY created_at",
            (user_id,),
        ).fetchall()
    return [
        {
            "cred_id": r["cred_id"],
            "created_at": r["created_at"],
            "last_used_at": r["last_used_at"],
            "transports": json.loads(r["transports"] or "[]"),
        }
        for r in rows
    ]
