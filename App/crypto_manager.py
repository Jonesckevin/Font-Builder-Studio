"""Signing, random tokens and at-rest encryption, all derived from SECRETS.

Uses ``itsdangerous`` (shipped with Flask) for signed time-limited tokens, the
stdlib ``secrets`` module for CSPRNG values and constant-time comparison, and
``cryptography``'s Fernet for encrypting data at rest.

If ``SECRETS`` is unset an ephemeral key is generated so local development keeps
working; set ``REQUIRE_SECRETS=true`` to make that a hard startup failure.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets as _secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

log = logging.getLogger("fontbuilder.crypto")

_SECRETS_KEY = os.environ.get("SECRETS", "").strip()
_EPHEMERAL = False
if not _SECRETS_KEY:
    _SECRETS_KEY = _secrets.token_hex(32)
    _EPHEMERAL = True
    log.warning("SECRETS is unset - using an ephemeral key (sessions will not survive restarts)")

_serializer = URLSafeTimedSerializer(_SECRETS_KEY, salt="font-builder-studio.v1")


def is_ephemeral() -> bool:
    """True when SECRETS was not provided and a throwaway key is in use."""
    return _EPHEMERAL


def has_secrets() -> bool:
    """True when a persistent SECRETS key is configured."""
    return not _EPHEMERAL


def generate_token(nbytes: int = 32) -> str:
    """A URL-safe, cryptographically-secure random token."""
    return _secrets.token_urlsafe(nbytes)


def sign(payload) -> str:
    """Serialise and sign a payload (str or JSON-serialisable) into a token."""
    return _serializer.dumps(payload)


def verify(token: str, max_age: int | None = None):
    """Return the payload if the token is valid and unexpired, else None."""
    if not token or not isinstance(token, str):
        return None
    try:
        return _serializer.loads(token, max_age=max_age)
    except SignatureExpired:
        log.info("Rejected expired token")
        return None
    except BadSignature:
        log.warning("Rejected token with invalid signature")
        return None


def constant_time_compare(a, b) -> bool:
    """Timing-attack-resistant comparison of two values."""
    return _secrets.compare_digest(str(a), str(b))


_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is None:
        from cryptography.fernet import Fernet

        key = base64.urlsafe_b64encode(hashlib.sha256(_SECRETS_KEY.encode("utf-8")).digest())
        _fernet = Fernet(key)
    return _fernet


def encrypt(plaintext) -> str:
    """URL-safe ciphertext for the given plaintext ('' for empty input)."""
    if plaintext is None or plaintext == "":
        return ""
    try:
        return _get_fernet().encrypt(str(plaintext).encode("utf-8")).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        log.error("Encryption failed: %s", exc)
        return ""


def decrypt(ciphertext) -> str | None:
    """Decrypted plaintext, or None when the token is unreadable."""
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(str(ciphertext).encode("utf-8")).decode("utf-8")
    except Exception:  # noqa: BLE001
        return None
