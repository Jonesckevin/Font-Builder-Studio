"""Feature flags and shared startup policy.

Kept in one module so ``app.py`` and the blueprints agree on the same values
without importing each other.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("fontbuilder.flags")


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# --- Authentication --------------------------------------------------------
ALLOW_AUTH = _bool("ALLOW_AUTH", False)
REQUIRE_AUTH = ALLOW_AUTH and _bool("REQUIRE_AUTH", False)
ALLOW_USER_REGISTRATION = ALLOW_AUTH and _bool("ALLOW_USER_REGISTRATION", True)
ALLOW_GUEST_LOGIN = ALLOW_AUTH and _bool("ALLOW_GUEST_LOGIN", True)
REQUIRE_SECRETS = _bool("REQUIRE_SECRETS", False)

# --- Proxy -----------------------------------------------------------------
TRUST_PROXY = _bool("TRUST_PROXY", False)

# --- WebAuthn (passkeys) ---------------------------------------------------
RP_ID = os.environ.get("RP_ID", "localhost").strip() or "localhost"
RP_NAME = os.environ.get("RP_NAME", "Font Builder Studio").strip() or "Font Builder Studio"

_origins = os.environ.get("ORIGIN", "").strip()
#: Accepted browser origins for WebAuthn. Empty means "derive from the request",
#: which is convenient for development but must be configured in production.
ORIGINS = [o.strip() for o in _origins.split(",") if o.strip()]


def check_startup_policy() -> None:
    """Fail fast on insecure combinations, warn about risky ones."""
    from crypto_manager import has_secrets  # local import to avoid a cycle

    if REQUIRE_SECRETS and not has_secrets():
        raise RuntimeError(
            "REQUIRE_SECRETS=true but SECRETS is not set. Set SECRETS (and "
            "SECRET_KEY) to a stable random value, or disable REQUIRE_SECRETS."
        )

    if REQUIRE_AUTH and not has_secrets():
        log.warning(
            "REQUIRE_AUTH is on but SECRETS is unset - an ephemeral signing key "
            "is in use, so every restart invalidates all sessions."
        )

    if ALLOW_AUTH and not ORIGINS:
        log.info(
            "ORIGIN is unset; WebAuthn origins will be derived from the request. "
            "Set ORIGIN to your exact site origin(s) in production."
        )
