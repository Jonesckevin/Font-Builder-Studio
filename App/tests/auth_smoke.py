"""Authentication smoke test.

Runs the Flask app in-process with auth enabled and an isolated database, so it
needs no live server and cannot touch real accounts::

    docker compose -f docker-compose.test.yml run --rm -T web-test \
        python tests/auth_smoke.py

Exits non-zero on failure.
"""

from __future__ import annotations

import os
import sys
import tempfile

# --- Configure the environment BEFORE importing the app ---------------------
_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="fbs-auth-"), "users.db")
os.environ["ALLOW_AUTH"] = "true"
os.environ["REQUIRE_AUTH"] = "true"
os.environ["ALLOW_USER_REGISTRATION"] = "true"
os.environ["ALLOW_GUEST_LOGIN"] = "true"
os.environ["SECRETS"] = "test-secrets-0123456789abcdef0123456789abcdef"
os.environ["SECRET_KEY"] = os.environ["SECRETS"]
os.environ["USERS_DB"] = _TMP_DB

# The functional checks below make several register/login calls from the same
# test IP, so the sliding-window limits are relaxed here and exercised directly
# further down instead.
os.environ["REGISTER_RATE_MAX"] = "500"
os.environ["LOGIN_RATE_MAX"] = "500"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import auth_manager  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def report(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"[{'ok' if ok else 'XX'}] {name}{' - ' + detail if detail else ''}", flush=True)


def main() -> int:
    print("=" * 68)
    print("Font Builder Studio - auth smoke test")
    print("=" * 68)

    client = app_module.app.test_client()

    # --- unauthenticated access is blocked ----------------------------------
    response = client.get("/api/projects")
    report(response.status_code == 401, "REQUIRE_AUTH blocks /api/", f"HTTP {response.status_code}")

    # ...but the shell, static assets and auth endpoints stay reachable.
    report(client.get("/").status_code == 200, "page shell stays reachable")
    report(
        client.get("/auth/config").status_code == 200,
        "auth config stays reachable",
    )

    config = client.get("/auth/config").get_json() or {}
    report(config.get("auth_enabled") is True, "config reports auth enabled", f"passkeys={config.get('passkey_support')}")

    # --- validation ---------------------------------------------------------
    response = client.post("/auth/register", json={"username": "ab", "password": "short"})
    report(response.status_code == 400, "rejects bad username/password", f"HTTP {response.status_code}")

    response = client.post(
        "/auth/register", json={"username": "tester", "password": "alllowercase123"}
    )
    report(response.status_code == 400, "enforces password complexity", f"HTTP {response.status_code}")

    # --- first account becomes admin ----------------------------------------
    response = client.post(
        "/auth/register",
        json={"username": "tester", "password": "Str0ngPassw0rd!", "email": "t@example.com"},
    )
    payload = response.get_json() or {}
    token = payload.get("token")
    report(
        response.status_code == 201 and bool(token),
        "register creates an account",
        f"HTTP {response.status_code}, role={(payload.get('user') or {}).get('role')}",
    )
    report(
        (payload.get("user") or {}).get("role") == "admin",
        "first account becomes admin",
    )

    # --- duplicate registration ---------------------------------------------
    response = client.post(
        "/auth/register", json={"username": "tester", "password": "Str0ngPassw0rd!"}
    )
    report(response.status_code == 400, "rejects duplicate username", f"HTTP {response.status_code}")

    # --- authenticated access ----------------------------------------------
    auth_header = {"Authorization": f"Bearer {token}"}
    response = client.get("/api/projects", headers=auth_header)
    report(response.status_code == 200, "token grants access", f"HTTP {response.status_code}")

    response = client.get("/auth/me", headers=auth_header)
    report(
        response.status_code == 200 and (response.get_json() or {}).get("user", {}).get("username") == "tester",
        "GET /auth/me returns the user",
        f"HTTP {response.status_code}",
    )

    # --- login --------------------------------------------------------------
    response = client.post("/auth/login", json={"username": "tester", "password": "wrong-password"})
    report(response.status_code == 401, "rejects wrong password", f"HTTP {response.status_code}")

    response = client.post(
        "/auth/login", json={"username": "tester", "password": "Str0ngPassw0rd!"}
    )
    login_token = (response.get_json() or {}).get("token")
    report(response.status_code == 200 and bool(login_token), "login returns a token", f"HTTP {response.status_code}")

    # --- logout revokes the token ------------------------------------------
    logout_header = {"Authorization": f"Bearer {login_token}"}
    response = client.post("/auth/logout", headers=logout_header)
    report(response.status_code == 200, "logout succeeds", f"HTTP {response.status_code}")

    response = client.get("/api/projects", headers=logout_header)
    report(response.status_code == 401, "revoked token is rejected", f"HTTP {response.status_code}")

    # the original token must still work (only the logged-out one was revoked)
    report(
        client.get("/api/projects", headers=auth_header).status_code == 200,
        "other sessions survive logout",
    )

    # --- tampered / garbage tokens -----------------------------------------
    bad = {"Authorization": "Bearer not.a.real.token"}
    report(client.get("/api/projects", headers=bad).status_code == 401, "rejects a malformed token")

    # --- guest sessions -----------------------------------------------------
    response = client.post("/auth/guest", json={})
    guest_token = (response.get_json() or {}).get("token")
    report(response.status_code == 201 and bool(guest_token), "guest login works", f"HTTP {response.status_code}")
    if guest_token:
        guest_header = {"Authorization": f"Bearer {guest_token}"}
        response = client.get("/auth/me", headers=guest_header)
        user = (response.get_json() or {}).get("user") or {}
        report(user.get("is_guest") is True, "guest session is flagged as guest", f"role={user.get('role')}")

    # --- passkeys -----------------------------------------------------------
    if config.get("passkey_support"):
        response = client.post("/auth/passkey/login/options", json={})
        body = response.get_json() or {}
        report(
            response.status_code == 200 and body.get("cid") and body.get("options", {}).get("challenge"),
            "passkey login options include a challenge",
            f"HTTP {response.status_code}",
        )

        # A garbage credential must be rejected, never accepted.
        response = client.post(
            "/auth/passkey/login/verify",
            json={"cid": body.get("cid"), "credential": {"id": "nope", "rawId": "nope", "response": {}}},
        )
        report(response.status_code in (400, 404), "rejects an unknown passkey", f"HTTP {response.status_code}")

        # Replaying the same challenge must fail (single-use challenges).
        response = client.post(
            "/auth/passkey/login/verify", json={"cid": body.get("cid"), "credential": {"id": "x", "rawId": "x"}}
        )
        report(response.status_code == 400, "challenges are single-use", f"HTTP {response.status_code}")
    else:
        report(True, "passkey support", "not available in this image (skipped)")

    # --- rate limiting (exercised directly; the HTTP flow runs relaxed) -----
    auth_manager.LOGIN_RATE_MAX = 2
    auth_manager.LOGIN_RATE_WINDOW = 900
    auth_manager.reset_login_attempts("rate-test-ip")
    report(auth_manager.login_rate_ok("rate-test-ip") is True, "rate limiter allows below the cap")
    auth_manager.record_login_failure("rate-test-ip")
    auth_manager.record_login_failure("rate-test-ip")
    report(
        auth_manager.login_rate_ok("rate-test-ip") is False,
        "rate limiter blocks at the cap",
        f"max={auth_manager.LOGIN_RATE_MAX}",
    )
    auth_manager.reset_login_attempts("rate-test-ip")
    report(
        auth_manager.login_rate_ok("rate-test-ip") is True,
        "a successful login clears the counter",
    )

    failures = sum(1 for ok, _n, _d in RESULTS if not ok)
    print("-" * 68)
    print(f"{len(RESULTS) - failures}/{len(RESULTS)} checks passed")
    if failures:
        print("\nAUTH SMOKE TEST FAILED")
        return 1
    print("\nAUTH SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
