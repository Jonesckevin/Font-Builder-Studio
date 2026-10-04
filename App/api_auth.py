"""Authentication API: passwords, guest sessions and WebAuthn passkeys.

Every route is inert (404) unless ``ALLOW_AUTH`` is enabled, so the default
single-user deployment has no auth surface at all.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid

from flask import Blueprint, jsonify, request

import auth_manager
import flags
import user_manager
from extensions import limiter

log = logging.getLogger("fontbuilder.auth.api")

bp = Blueprint("auth_api", __name__, url_prefix="/auth")

# --- WebAuthn (optional dependency) ----------------------------------------
try:
    import webauthn as _webauthn
    from webauthn.helpers import base64url_to_bytes as _b64url_to_bytes
    from webauthn.helpers import bytes_to_base64url as _bytes_to_b64url
    from webauthn.helpers.structs import (
        AuthenticatorAssertionResponse,
        AuthenticatorAttestationResponse,
        AuthenticatorSelectionCriteria,
        AuthenticatorTransport,
        AuthenticationCredential,
        RegistrationCredential,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    PASSKEY_SUPPORT = True
except ImportError:  # pragma: no cover - depends on the image
    PASSKEY_SUPPORT = False
    log.info("webauthn is not installed - passkey endpoints will return 501")

# --- Short-lived WebAuthn challenges ---------------------------------------
#: Challenges are single-use (popped on read) and expire, which prevents replay.
_CHALLENGE_TTL = 300
_CHALLENGE_MAX = 500
_challenges: dict[str, tuple[float, dict]] = {}
_challenge_lock = threading.Lock()


def _challenge_put(payload: dict) -> str:
    cid = uuid.uuid4().hex
    now = time.time()
    with _challenge_lock:
        for key, (stamp, _) in list(_challenges.items()):
            if now - stamp > _CHALLENGE_TTL:
                del _challenges[key]
        if len(_challenges) >= _CHALLENGE_MAX:
            oldest = min(_challenges, key=lambda k: _challenges[k][0])
            del _challenges[oldest]
        _challenges[cid] = (now, payload)
    return cid


def _challenge_take(cid):
    if not cid:
        return None
    with _challenge_lock:
        entry = _challenges.pop(cid, None)
    if not entry:
        return None
    stamp, payload = entry
    if time.time() - stamp > _CHALLENGE_TTL:
        return None
    return payload


def _origin() -> str:
    """Expected WebAuthn origin.

    Prefers the configured ORIGIN; falls back to the request's own host, which
    is convenient for local development (and is logged as a warning at startup).
    """
    if flags.ORIGINS:
        return flags.ORIGINS[0]
    return request.host_url.rstrip("/")


def _error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def _auth_disabled():
    return _error("Authentication is disabled", 404)


def _public_user(user: dict) -> dict:
    return {
        "user_id": user.get("sub") or user.get("user_id"),
        "username": user.get("username"),
        "role": user.get("role"),
        "is_guest": bool(user.get("is_guest", False)),
    }


# ============================================================================
# Capabilities
# ============================================================================
@bp.get("/config")
def auth_config():
    """Feature discovery for the UI. Always reachable."""
    return jsonify(
        {
            "ok": True,
            "auth_enabled": flags.ALLOW_AUTH,
            "registration_enabled": flags.ALLOW_USER_REGISTRATION,
            "guest_login_enabled": flags.ALLOW_GUEST_LOGIN,
            "require_auth": flags.REQUIRE_AUTH,
            "passkey_support": PASSKEY_SUPPORT,
        }
    )


@bp.get("/me")
def auth_me():
    if not flags.ALLOW_AUTH:
        return _auth_disabled()
    user = auth_manager.get_current_user()
    if not user:
        return _error("Authentication required", 401)
    return jsonify({"ok": True, "user": _public_user(user)})


@bp.get("/passkeys")
@auth_manager.login_required
def list_passkeys():
    if not flags.ALLOW_AUTH:
        return _auth_disabled()
    user = auth_manager.get_current_user()
    return jsonify({"ok": True, "passkeys": user_manager.list_user_passkeys(user["sub"])})


# ============================================================================
# Password auth
# ============================================================================
@bp.post("/register")
@limiter.limit("20 per hour")
def auth_register():
    if not flags.ALLOW_AUTH or not flags.ALLOW_USER_REGISTRATION:
        return _error("Registration is disabled", 404)

    ip = auth_manager.client_ip()
    if not auth_manager.register_rate_ok(ip):
        return _error("Too many registration attempts, please try again later", 429)
    auth_manager.record_register_attempt(ip)

    data = request.get_json(silent=True) or {}
    try:
        # The very first account becomes the admin.
        role = "admin" if user_manager.count_users() == 0 else "user"
        user = user_manager.create_user(
            data.get("username"), data.get("password"), data.get("email"), role=role
        )
    except ValueError as exc:
        return _error(str(exc))

    token, _exp = auth_manager.create_access_token(user["user_id"], user["role"], user["username"])
    log.info("registered %r (role=%s)", user["username"], user["role"])
    return jsonify({"ok": True, "user": user, "token": token}), 201


@bp.post("/login")
@limiter.limit("30 per minute")
def auth_login():
    if not flags.ALLOW_AUTH:
        return _auth_disabled()

    ip = auth_manager.client_ip()
    if not auth_manager.login_rate_ok(ip):
        return _error("Too many login attempts, please try again later", 429)

    data = request.get_json(silent=True) or {}
    user = user_manager.authenticate(data.get("username"), data.get("password"))
    if not user:
        auth_manager.record_login_failure(ip)
        log.warning("failed login for %r from %s", data.get("username"), ip)
        return _error("Invalid credentials", 401)

    auth_manager.reset_login_attempts(ip)
    token, _exp = auth_manager.create_access_token(user["user_id"], user["role"], user["username"])
    log.info("login success %r", user["username"])
    return jsonify({"ok": True, "user": user, "token": token})


@bp.post("/logout")
def auth_logout():
    if not flags.ALLOW_AUTH:
        return _auth_disabled()
    token = auth_manager.extract_token()
    if token:
        claims = auth_manager.decode_token(token)
        if claims and claims.get("jti"):
            user_manager.revoke_token(claims["jti"], claims.get("exp", 0))
    return jsonify({"ok": True})


@bp.post("/guest")
@limiter.limit("30 per hour")
def auth_guest():
    if not flags.ALLOW_AUTH or not flags.ALLOW_GUEST_LOGIN:
        return _error("Guest login is disabled", 404)

    user = user_manager.create_guest_user()
    token, _exp = auth_manager.create_access_token(
        user["user_id"], user["role"], user["username"], extra_claims={"is_guest": True}
    )
    log.info("guest session created: %s", user["username"])
    return jsonify({"ok": True, "user": user, "token": token}), 201


# ============================================================================
# Passkeys (WebAuthn)
# ============================================================================
def _require_passkeys():
    if not PASSKEY_SUPPORT:
        return _error("Passkey support is not available on this server", 501)
    return None


@bp.post("/passkey/register/options")
@limiter.limit("30 per hour")
def passkey_register_options():
    if not flags.ALLOW_AUTH or not flags.ALLOW_USER_REGISTRATION:
        return _error("Registration is disabled", 404)
    unavailable = _require_passkeys()
    if unavailable:
        return unavailable

    ip = auth_manager.client_ip()
    if not auth_manager.register_rate_ok(ip):
        return _error("Too many registration attempts, please try again later", 429)

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip() or None

    err = user_manager.validate_username(username)
    if err:
        return _error(err)

    existing = user_manager.get_user_by_username(username)
    if existing:
        # Prevent account takeover: adding a passkey to an existing account
        # requires being logged in as that exact account.
        current = auth_manager.get_current_user()
        if not current or current.get("sub") != existing["user_id"]:
            return _error("Username already exists")
        uid = existing["user_id"]
        is_new_user = False
    else:
        uid = str(uuid.uuid4())
        is_new_user = True

    options = _webauthn.generate_registration_options(
        rp_id=flags.RP_ID,
        rp_name=flags.RP_NAME,
        user_id=bytes.fromhex(uid.replace("-", "")),
        user_name=username,
        user_display_name=username,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    cid = _challenge_put(
        {"challenge": options.challenge, "username": username, "email": email,
         "uid": uid, "is_new_user": is_new_user}
    )
    return jsonify({"ok": True, "cid": cid, "options": json.loads(_webauthn.options_to_json(options))})


@bp.post("/passkey/register/verify")
def passkey_register_verify():
    if not flags.ALLOW_AUTH or not flags.ALLOW_USER_REGISTRATION:
        return _error("Registration is disabled", 404)
    unavailable = _require_passkeys()
    if unavailable:
        return unavailable

    ip = auth_manager.client_ip()
    data = request.get_json(silent=True) or {}
    challenge = _challenge_take(data.get("cid"))
    if not challenge:
        return _error("Challenge expired - please try again")

    cred_data = data.get("credential")
    if not isinstance(cred_data, dict):
        return _error("Missing credential")

    try:
        response = cred_data.get("response", {})
        transports = []
        for name in response.get("transports") or []:
            try:
                transports.append(AuthenticatorTransport(name))
            except ValueError:
                pass
        credential = RegistrationCredential(
            id=cred_data["id"],
            raw_id=_b64url_to_bytes(cred_data["rawId"]),
            response=AuthenticatorAttestationResponse(
                client_data_json=_b64url_to_bytes(response["clientDataJSON"]),
                attestation_object=_b64url_to_bytes(response["attestationObject"]),
                transports=transports,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("passkey register: bad credential (%s)", exc)
        return _error("Invalid credential format")

    try:
        verification = _webauthn.verify_registration_response(
            credential=credential,
            expected_challenge=challenge["challenge"],
            expected_rp_id=flags.RP_ID,
            expected_origin=_origin(),
            require_user_verification=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("passkey register: verification failed (%s)", exc)
        return _error(f"Passkey verification failed: {exc}")

    if challenge["is_new_user"]:
        auth_manager.record_register_attempt(ip)
        try:
            role = "admin" if user_manager.count_users() == 0 else "user"
            user = user_manager.create_user_passwordless(
                challenge["username"], challenge.get("email"), role=role, forced_id=challenge["uid"]
            )
        except ValueError as exc:
            return _error(str(exc))
    else:
        row = user_manager.get_user_by_id(challenge["uid"])
        if not row:
            return _error("User not found", 404)
        user = user_manager._public(row)

    try:
        user_manager.add_passkey(
            user["user_id"],
            _bytes_to_b64url(verification.credential_id),
            _bytes_to_b64url(verification.credential_public_key),
            verification.sign_count,
            [t.value if hasattr(t, "value") else str(t) for t in transports],
        )
    except Exception as exc:  # noqa: BLE001
        log.error("passkey register: could not store credential (%s)", exc)
        return _error("Failed to store passkey", 500)

    token, _exp = auth_manager.create_access_token(user["user_id"], user["role"], user["username"])
    log.info("passkey registered for %r", user["username"])
    return jsonify({"ok": True, "user": user, "token": token}), (201 if challenge["is_new_user"] else 200)


@bp.post("/passkey/login/options")
@limiter.limit("60 per hour")
def passkey_login_options():
    if not flags.ALLOW_AUTH:
        return _auth_disabled()
    unavailable = _require_passkeys()
    if unavailable:
        return unavailable

    options = _webauthn.generate_authentication_options(
        rp_id=flags.RP_ID,
        user_verification=UserVerificationRequirement.PREFERRED,
        allow_credentials=[],
    )
    cid = _challenge_put({"challenge": options.challenge})
    return jsonify({"ok": True, "cid": cid, "options": json.loads(_webauthn.options_to_json(options))})


@bp.post("/passkey/login/verify")
@limiter.limit("60 per hour")
def passkey_login_verify():
    if not flags.ALLOW_AUTH:
        return _auth_disabled()
    unavailable = _require_passkeys()
    if unavailable:
        return unavailable

    ip = auth_manager.client_ip()
    if not auth_manager.login_rate_ok(ip):
        return _error("Too many login attempts, please try again later", 429)

    data = request.get_json(silent=True) or {}
    challenge = _challenge_take(data.get("cid"))
    if not challenge:
        return _error("Challenge expired - please try again")

    cred_data = data.get("credential")
    if not isinstance(cred_data, dict):
        return _error("Missing credential")

    passkey = user_manager.get_passkey(cred_data.get("id"))
    if not passkey:
        return _error("Unknown passkey - please register first", 404)

    try:
        response = cred_data.get("response", {})
        credential = AuthenticationCredential(
            id=cred_data["id"],
            raw_id=_b64url_to_bytes(cred_data["rawId"]),
            response=AuthenticatorAssertionResponse(
                client_data_json=_b64url_to_bytes(response["clientDataJSON"]),
                authenticator_data=_b64url_to_bytes(response["authenticatorData"]),
                signature=_b64url_to_bytes(response["signature"]),
                user_handle=_b64url_to_bytes(response["userHandle"]) if response.get("userHandle") else None,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("passkey login: bad credential (%s)", exc)
        return _error("Invalid credential format")

    try:
        verification = _webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=challenge["challenge"],
            expected_rp_id=flags.RP_ID,
            expected_origin=_origin(),
            credential_public_key=_b64url_to_bytes(passkey["public_key"]),
            credential_current_sign_count=passkey["sign_count"],
            require_user_verification=False,
        )
    except Exception as exc:  # noqa: BLE001
        auth_manager.record_login_failure(ip)
        log.warning("passkey login: verification failed (%s)", exc)
        return _error("Passkey authentication failed", 401)

    user_manager.update_passkey_sign_count(passkey["cred_id"], verification.new_sign_count)

    row = user_manager.get_user_by_id(passkey["user_id"])
    if not row:
        return _error("User not found", 404)
    user = user_manager._public(row)
    if user["status"] != "active":
        return _error("Account is not active", 403)

    auth_manager.reset_login_attempts(ip)
    token, _exp = auth_manager.create_access_token(user["user_id"], user["role"], user["username"])
    log.info("passkey login %r", user["username"])
    return jsonify({"ok": True, "user": user, "token": token})
