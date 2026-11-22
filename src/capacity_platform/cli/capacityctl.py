"""`capacityctl` — operator CLI over the platform API (S27 §4).

Login is OIDC Authorization Code + PKCE against Keycloak with a one-shot loopback redirect
on 127.0.0.1; the CLI is a public client and embeds no secret. Tokens are stored per user
and, on Windows, encrypted with DPAPI (CryptProtectData) so another account cannot read
them. Tokens are never printed. `plans approve` shows the exact target, before/after values
and digest and asks for explicit confirmation; being an administrator changes nothing.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from pathlib import Path
from typing import Any

import httpx
import typer

app = typer.Typer(no_args_is_help=True, help="Operate the capacity platform (Observe / Advisory / Governed).")
auth_app = typer.Typer(no_args_is_help=True)
plans_app = typer.Typer(no_args_is_help=True)
analysis_app = typer.Typer(no_args_is_help=True)
app.add_typer(auth_app, name="auth")
app.add_typer(plans_app, name="plans")
app.add_typer(analysis_app, name="analysis")

HOME = Path(os.environ.get("CAPACITY_LAB_HOME", Path.home() / ".capacity-lab"))
API = os.environ.get("CAPACITY_API_URL", "https://localhost:8443")
ISSUER = os.environ.get("CAPACITY_OIDC_ISSUER", "https://localhost:8444/realms/capacity")
CLIENT_ID = "capacityctl"
CA_BUNDLE = os.environ.get("CAPACITY_CA_BUNDLE", str(HOME / "pki" / "root-ca.crt"))
TOKEN_FILE = HOME / "capacityctl" / "token.bin"


# ------------------------------------------------------------------ protected token storage
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        return data  # POSIX: rely on 0600 file permissions (documented limitation)
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    src = _Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise typer.BadParameter("token store could not be (un)protected for this Windows user")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)  # type: ignore[attr-defined]


def _save(tokens: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_bytes(_dpapi(json.dumps(tokens).encode(), protect=True))
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass


def _load() -> dict | None:
    if not TOKEN_FILE.exists():
        return None
    return json.loads(_dpapi(TOKEN_FILE.read_bytes(), protect=False))


def _access_token() -> str:
    tokens = _load()
    if not tokens:
        typer.echo("not logged in: run `capacityctl auth login`", err=True)
        raise typer.Exit(10)
    if tokens.get("expires_at", 0) - 30 < time.time():
        tokens = _refresh(tokens)
    return tokens["access_token"]


def _refresh(tokens: dict) -> dict:
    meta = _discovery()
    resp = httpx.post(
        meta["token_endpoint"],
        verify=CA_BUNDLE,
        data={"grant_type": "refresh_token", "refresh_token": tokens.get("refresh_token", ""), "client_id": CLIENT_ID},
    )
    if resp.status_code != 200:
        typer.echo("session expired: run `capacityctl auth login`", err=True)
        raise typer.Exit(10)
    new = resp.json()
    new["expires_at"] = time.time() + new.get("expires_in", 300)
    _save(new)
    return new


def _discovery() -> dict:
    return httpx.get(f"{ISSUER}/.well-known/openid-configuration", verify=CA_BUNDLE, timeout=10).json()


# ------------------------------------------------------------------ auth commands
@auth_app.command("login")
def login(no_browser: bool = False) -> None:
    meta = _discovery()
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Login complete. You can close this window.")

        def log_message(self, *args: Any) -> None:
            pass

    # Loopback only; the port matches the redirect URI registered for the public client.
    server = http.server.HTTPServer(("127.0.0.1", int(os.environ.get("CAPACITYCTL_CALLBACK_PORT", "8765"))), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}/callback"
    url = (
        meta["authorization_endpoint"]
        + "?"
        + urllib.parse.urlencode(
            {
                "client_id": CLIENT_ID,
                "response_type": "code",
                "redirect_uri": redirect,
                "scope": "openid",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    if no_browser:
        typer.echo(f"Open this URL to log in:\n{url}")
    else:
        webbrowser.open(url)
    thread.join(timeout=300)
    server.server_close()
    if result.get("state") != state or "code" not in result:
        typer.echo("login failed: state mismatch or no authorization code", err=True)
        raise typer.Exit(10)
    resp = httpx.post(
        meta["token_endpoint"],
        verify=CA_BUNDLE,
        data={
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": redirect,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        },
    )
    if resp.status_code != 200:
        typer.echo(f"token exchange failed ({resp.status_code})", err=True)
        raise typer.Exit(10)
    tokens = resp.json()
    tokens["expires_at"] = time.time() + tokens.get("expires_in", 300)
    _save(tokens)
    typer.echo("logged in")


@auth_app.command("status")
def auth_status() -> None:
    tokens = _load()
    if not tokens:
        typer.echo("not logged in")
        raise typer.Exit(10)
    remaining = int(tokens.get("expires_at", 0) - time.time())
    typer.echo(f"access token valid for {max(remaining, 0)} s (contents not shown)")


@auth_app.command("logout")
def logout() -> None:
    TOKEN_FILE.unlink(missing_ok=True)
    typer.echo("logged out; local tokens removed")


# ------------------------------------------------------------------ API helper
def _call(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:
    resp = httpx.request(
        method,
        API + path,
        json=body,
        verify=CA_BUNDLE,
        timeout=30,
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "X-Correlation-ID": str(uuid.uuid4()),
            **(headers or {}),
        },
    )
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400:
        err = data.get("error", {})
        typer.echo(
            f"error {resp.status_code} {err.get('code')}: {err.get('message')} "
            f"(correlation {err.get('correlationId')})",
            err=True,
        )
        raise typer.Exit({401: 10, 403: 11, 409: 12, 422: 13, 503: 14}.get(resp.status_code, 1))
    return data


def _out(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


@app.command("capabilities")
def capabilities() -> None:
    _out(_call("GET", "/api/v1/capabilities"))


@app.command("workloads")
def workloads() -> None:
    _out(_call("GET", "/api/v1/workloads"))


@app.command("nodes")
def nodes() -> None:
    _out(_call("GET", "/api/v1/nodes"))


@analysis_app.command("create")
def analysis_create(workload: str, horizon: str = "1h", advisory_only: bool = False) -> None:
    _out(
        _call(
            "POST",
            "/api/v1/analyses",
            {
                "workloadId": workload,
                "horizon": horizon,
                "intendedUse": "advisory" if advisory_only else "governed_execution",
            },
            {"Idempotency-Key": str(uuid.uuid4())},
        )
    )


@analysis_app.command("show")
def analysis_show(digest: str) -> None:
    _out(_call("GET", f"/api/v1/analyses/{digest}"))


@app.command("operation")
def operation(operation_id: str, watch: bool = False) -> None:
    while True:
        data = _call("GET", f"/api/v1/operations/{operation_id}")
        if not watch or data["state"] in ("completed", "failed", "cancelled", "timed_out"):
            _out(data)
            return
        time.sleep(3)


@app.command("advisory")
def advisory(
    workload: str, mode: str = "diagnostic", analysis: str | None = None, evaluation: str | None = None
) -> None:
    _out(
        _call(
            "POST",
            "/api/v1/advisories",
            {"workloadId": workload, "mode": mode, "analysisDigest": analysis, "evaluationDigest": evaluation},
        )
    )


@plans_app.command("create")
def plan_create(evaluation: str, advisory_artifact: str | None = None) -> None:
    _out(_call("POST", "/api/v1/plans", {"evaluationDigest": evaluation, "advisoryArtifactDigest": advisory_artifact}))


@plans_app.command("show")
def plan_show(plan_id: str, revision: int | None = None) -> None:
    _out(_call("GET", f"/api/v1/plans/{plan_id}" + (f"?revision={revision}" if revision else "")))


@plans_app.command("approve")
def plan_approve(
    plan_id: str,
    revision: int = typer.Option(...),
    digest: str = typer.Option(...),
    reject: bool = False,
    yes: bool = False,
) -> None:
    current = _call("GET", f"/api/v1/plans/{plan_id}?revision={revision}")
    plan = current["plan"]
    target = plan["target"]
    typer.echo(
        f"Plan {plan_id} r{revision} state={current['state']}\n"
        f"  target: {target['namespace']}/{target['controller']['name']} uid={target['controller']['uid']} "
        f"container={target['containerName']}"
    )
    for change in plan["approvedFieldChanges"]:
        typer.echo(f"  {change['field']}: {change['before']} -> {change['after']}")
    typer.echo(f"  digest: {plan['planDigest']}\n  expires: {plan['expiresAtUtc']}")
    if plan["planDigest"] != digest:
        typer.echo("the digest you supplied does not match this revision; refusing", err=True)
        raise typer.Exit(12)
    verb = "REJECT" if reject else "APPROVE"
    if not yes and not typer.confirm(f"{verb} exactly this revision?"):
        raise typer.Exit(1)
    _out(
        _call(
            "POST",
            f"/api/v1/plans/{plan_id}/approvals",
            {"revision": revision, "planDigest": digest, "decision": "reject" if reject else "approve"},
        )
    )


@plans_app.command("execution-request")
def plan_execute(plan_id: str, revision: int = typer.Option(...)) -> None:
    _out(
        _call(
            "POST",
            f"/api/v1/plans/{plan_id}/execution-requests",
            {"revision": revision},
            {"Idempotency-Key": f"{plan_id}-r{revision}"},
        )
    )


@plans_app.command("revoke")
def plan_revoke(plan_id: str, revision: int = typer.Option(...), reason: str = typer.Option(...)) -> None:
    _out(_call("POST", f"/api/v1/plans/{plan_id}/revocations", {"revision": revision, "reason": reason}))


@app.command("execution")
def execution(intent_id: str) -> None:
    _out(_call("GET", f"/api/v1/executions/{intent_id}"))


@app.command("mode")
def mode(new_mode: str, expected_revision: int = typer.Option(...)) -> None:
    _out(_call("PUT", "/api/v1/mode", {"mode": new_mode, "expectedRevision": expected_revision}))


if __name__ == "__main__":
    app(sys.argv[1:])
