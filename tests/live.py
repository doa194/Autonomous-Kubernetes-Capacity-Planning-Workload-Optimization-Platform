"""Helpers shared by the operational and end-to-end suites that run against the live kind lab.

Both suites skip (never pass) when the lab is not reachable, so a missing environment shows up
as `skipped` in the junit evidence and as `not_run` in the release report.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import httpx
import pytest

HOME = Path.home() / ".capacity-lab"
CA = str(HOME / "pki" / "root-ca.crt")
API = "https://localhost:8443"
ISSUER = "https://localhost:8444/realms/capacity"
CONTEXT = "kind-capacity-lab"


def require_lab() -> None:
    try:
        proc = subprocess.run(
            ["kubectl", "--context", CONTEXT, "get", "--raw", "/readyz"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("kind lab not reachable")
    if proc.returncode != 0 or proc.stdout.strip() != "ok":
        pytest.skip("kind lab not reachable")


def kubectl(*args: str, timeout: int = 120, check: bool = True) -> str:
    proc = subprocess.run(
        ["kubectl", "--context", CONTEXT, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"kubectl {' '.join(args[:3])} failed: {proc.stderr[-400:]}")
    return proc.stdout


_TOKENS: dict[str, tuple[str, float]] = {}


def token(user: str) -> str:
    """Access token for a lab test user via the TEST-ONLY `capacity-e2e` direct-grant client.

    Cached until a minute before it expires. Callers ask again at each use: the E2E test waits
    about 20 minutes for verification, far longer than one access token lives.
    """
    from capacity_platform.lab.identity import ensure_passwords

    cached = _TOKENS.get(user)
    if cached and cached[1] - time.monotonic() > 60:
        return cached[0]

    resp = httpx.post(
        f"{ISSUER}/protocol/openid-connect/token",
        verify=CA,
        timeout=20,
        data={
            "grant_type": "password",
            "client_id": "capacity-e2e",
            "username": user,
            "password": ensure_passwords()[user],
            "scope": "openid",
        },
    )
    assert resp.status_code == 200, f"token for {user}: {resp.status_code}"
    body = resp.json()
    _TOKENS[user] = (body["access_token"], time.monotonic() + float(body.get("expires_in", 300)))
    return body["access_token"]


def api(method: str, path: str, user_token: str | None = None, **kwargs) -> httpx.Response:
    headers = kwargs.pop("headers", {})
    if user_token:
        headers["Authorization"] = f"Bearer {user_token}"
    return httpx.request(method, f"{API}{path}", headers=headers, verify=CA, timeout=60, **kwargs)


def wait_until(predicate, timeout: float, interval: float = 5.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}")
