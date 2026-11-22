"""S27/S06 component tests: real FastAPI apps, real PostgreSQL, production token validators."""

import pytest
from fastapi.testclient import TestClient
from tests.auth_helpers import ISSUER, SA_ISSUER, TestKeys

from capacity_platform.api.app import Relay, build_app
from capacity_platform.contracts.enums import HumanRole
from capacity_platform.governance.authz import Grant
from capacity_platform.governance.service import GovernanceService
from capacity_platform.governance.service import build_app as build_governance
from capacity_platform.governance.store import GovernanceStore
from capacity_platform.shared.auth.oidc import AccessTokenValidator, OidcSettings
from capacity_platform.shared.auth.service_identity import ServiceTokenVerifier
from capacity_platform.shared.db import connect, transaction

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    return TestKeys(tmp_path_factory.mktemp("keys"))


@pytest.fixture(scope="module")
def validator(keys):
    return AccessTokenValidator(
        OidcSettings(
            issuer=ISSUER,
            audience="capacity-api",
            allowed_clients=frozenset({"capacityctl"}),
            jwks_file=str(keys.jwks_file),
        )
    )


@pytest.fixture(scope="module")
def api(role_dsn, validator):
    with connect(role_dsn("svc_governance")) as conn, transaction(conn):
        store = GovernanceStore(conn)
        store.add_grant(
            Grant(
                "api-viewer",
                f"human:{ISSUER}#viewer-batch",
                HumanRole.VIEWER,
                "capacity-lab",
                frozenset({"workloads-batch"}),
                1,
            ),
            "bootstrap",
        )
        store.add_grant(
            Grant(
                "api-analyst",
                f"human:{ISSUER}#analyst",
                HumanRole.ANALYST,
                "capacity-lab",
                frozenset({"workloads-batch"}),
                1,
            ),
            "bootstrap",
        )
    app = build_app(
        lambda: connect(role_dsn("svc_api")), validator, Relay("http://gov.invalid", "http://ai.invalid", None)
    )
    return TestClient(app)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_missing_token_is_401_with_typed_envelope(api):
    resp = api.get("/api/v1/workloads")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "AUTHENTICATION_REQUIRED" and body["error"]["correlationId"]


@pytest.mark.parametrize(
    "variant", ["id_token", "wrong_audience", "expired", "foreign_key", "wrong_client", "wrong_issuer"]
)
def test_invalid_tokens_are_rejected(api, keys, variant):
    kwargs = {
        "id_token": {"typ": "ID"},
        "wrong_audience": {"aud": "account"},
        "expired": {"exp_offset": -3600},
        "foreign_key": {"key": keys.attacker},
        "wrong_client": {"azp": "evil-cli"},
        "wrong_issuer": {"iss": "https://evil.example/realms/capacity"},
    }[variant]
    resp = api.get("/api/v1/workloads", headers=bearer(keys.human("viewer-batch", ["Viewer"], **kwargs)))
    assert resp.status_code == 401


def test_alg_none_token_is_rejected(api):
    import base64
    import json

    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "kid": "test-key"}).encode()).rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps({"iss": ISSUER, "sub": "x", "aud": "capacity-api"}).encode()).rstrip(
        b"="
    )
    resp = api.get("/api/v1/workloads", headers=bearer(f"{header.decode()}.{body.decode()}."))
    assert resp.status_code == 401


def test_viewer_sees_only_granted_namespaces(api, keys):
    resp = api.get("/api/v1/workloads", headers=bearer(keys.human("viewer-batch", ["Viewer"])))
    assert resp.status_code == 200
    namespaces = {w["namespace"] for w in resp.json()["data"]}
    assert namespaces == {"workloads-batch"}  # critical/standard omitted without count leak


def test_viewer_cannot_request_analysis(api, keys):
    resp = api.post(
        "/api/v1/analyses",
        json={"workloadId": "w04-cpu-worker"},
        headers=bearer(keys.human("viewer-batch", ["Viewer"])),
    )
    assert resp.status_code == 403


def test_analysis_request_is_async_and_idempotent(api, keys):
    headers = {**bearer(keys.human("analyst", ["Analyst"])), "Idempotency-Key": "same-request"}
    first = api.post("/api/v1/analyses", json={"workloadId": "w04-cpu-worker"}, headers=headers)
    second = api.post("/api/v1/analyses", json={"workloadId": "w04-cpu-worker"}, headers=headers)
    assert first.status_code == 202 and first.json()["state"] == "queued"
    assert first.json()["operationId"] == second.json()["operationId"]
    status = api.get(first.json()["statusUrl"], headers=bearer(keys.human("analyst", ["Analyst"])))
    assert status.status_code == 200 and "not an approved or executed change" in status.json()["note"]


def test_out_of_scope_analysis_and_unknown_operation_are_opaque(api, keys):
    token = bearer(keys.human("analyst", ["Analyst"]))
    out_of_scope = api.post("/api/v1/analyses", json={"workloadId": "w01-latency-api"}, headers=token)
    missing = api.get("/api/v1/operations/00000000-0000-0000-0000-000000000000", headers=token)
    assert out_of_scope.status_code == 403 and missing.status_code == 403
    assert out_of_scope.json()["error"]["message"] == missing.json()["error"]["message"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/kubectl",
        "/api/v1/sql",
        "/api/v1/promql",
        "/api/v1/patch",
        "/api/v1/agent/tool/execute",
        "/api/v1/ollama",
    ],
)
def test_no_generic_relay_endpoints_exist(api, keys, path):
    assert api.post(path, json={}, headers=bearer(keys.human("analyst", ["Analyst"]))).status_code in (404, 405)


def test_arbitrary_request_fields_are_rejected(api, keys):
    resp = api.post(
        "/api/v1/analyses",
        json={"workloadId": "w04-cpu-worker; rm -rf /"},
        headers=bearer(keys.human("analyst", ["Analyst"])),
    )
    assert resp.status_code == 422


@pytest.fixture(scope="module")
def governance(role_dsn, validator, keys):
    machines = ServiceTokenVerifier(issuer=SA_ISSUER, jwks_file=str(keys.jwks_file))
    return TestClient(
        build_governance(GovernanceService(lambda: connect(role_dsn("svc_governance"))), validator, machines)
    )


def test_governance_requires_api_machine_identity(governance, keys):
    user = bearer(keys.human("analyst", ["Planner"]))
    assert governance.get("/internal/v1/plans/plan-x", headers=user).status_code == 401
    wrong_sa = {**user, "X-Service-Token": keys.service("capacity-ai", "capacity-ai-sa")}
    assert governance.get("/internal/v1/plans/plan-x", headers=wrong_sa).status_code == 403


def test_forged_user_header_does_not_authenticate(governance, keys):
    headers = {
        "X-Service-Token": keys.service("capacity-api", "capacity-api-sa"),
        "X-User": "sub-approver",
        "X-Roles": "Approver",
    }
    assert governance.get("/internal/v1/plans/plan-x", headers=headers).status_code == 401
