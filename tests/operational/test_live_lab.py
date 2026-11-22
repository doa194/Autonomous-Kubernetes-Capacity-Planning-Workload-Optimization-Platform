"""Operational checks against the live kind lab (S30 infrastructure layer, S28/S29 recovery).

These verify behaviour only the real platform can show: enforced network isolation, RBAC and
admission on the real API server, real TLS + OIDC wiring, the model-gateway boundary, a backup
that actually restores, the audit chain against signed checkpoints, and recovery after losing
the executor Pod. Business rules are covered by the unit/integration suites, not repeated here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from tests.live import HOME, api, kubectl, require_lab, token, wait_until

# Live checks start probe Pods and containers; on a hard-disk host they can take many minutes.
pytestmark = [pytest.mark.operational, pytest.mark.timeout(1800)]

PLATFORM = {
    "capacity-api": "capacity-api",
    "capacity-observer": "capacity-core",
    "capacity-capacity": "capacity-core",
    "capacity-governance": "capacity-core",
    "capacity-ai": "capacity-ai",
    "capacity-executor": "capacity-execution",
}


@pytest.fixture(scope="module", autouse=True)
def lab():
    require_lab()


@pytest.fixture(scope="module")
def verification():
    """One full `capacity-lab verify all` pass, kept as release evidence like the CLI does."""
    from capacity_platform.lab.verify import run_checks

    report = run_checks("all")
    out = HOME / "evidence" / f"verify-all-{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    return report["checks"]


@pytest.mark.parametrize(
    "check",
    [
        "network_default_deny_enforced",
        "rbac_least_privilege",
        "admission_negative_cases",
        "executor_field_guard",
        "observability_targets",
        "placement_pools",
        "topology_and_six_deployments",
    ],
)
def test_live_verification_check_passes(verification, check):
    result = verification[check]
    assert result["verdict"] == "pass", json.dumps(result, default=str)[:1500]


def test_six_platform_deployments_are_available():
    for name, namespace in PLATFORM.items():
        dep = json.loads(kubectl("-n", namespace, "get", "deployment", name, "-o", "json"))
        assert dep["status"].get("availableReplicas", 0) >= 1, name


def test_api_requires_a_valid_user_token_over_tls():
    assert api("GET", "/api/v1/workloads").status_code == 401
    assert api("GET", "/api/v1/workloads", "not-a-jwt").status_code == 401
    assert api("GET", "/api/v1/workloads", token("viewer1")).status_code == 200


def test_model_gateway_is_reachable_from_ai_but_not_from_demo_pods():
    from capacity_platform.lab.verify import _probe, _python_digest

    probe = (
        "import httpx; print(httpx.get('https://host.docker.internal:11500/healthz', "
        "verify='/var/run/secrets/capacity/ca/ca.crt', timeout=5).status_code)"
    )
    from_ai = kubectl("-n", "capacity-ai", "exec", "deploy/capacity-ai", "--", "python", "-c", probe, check=False)
    if from_ai.strip() != "200":
        pytest.skip("model gateway not running on the host (start `capacity-lab model gateway`)")
    host = json.loads((HOME / "release" / "network.json").read_text())["hostGatewayIp"]
    # A demo workload Pod must not reach the gateway at all (S15-TEST-006).
    assert _probe("workloads-standard", {"app": "gateway-bypass-probe"}, host, 11500, _python_digest()) == "TIMEOUT"


def test_backup_restores_into_an_isolated_instance():
    from capacity_platform.lab.backup import create, verify_restore

    manifest = create()
    result = verify_restore(str(HOME / "backups" / manifest["backupId"]))
    assert result["restored"], result["checks"]


def test_audit_chain_matches_signed_checkpoints():
    from capacity_platform.lab.backup import audit_checkpoint, audit_verify

    audit_checkpoint()
    result = audit_verify()
    assert result["intact"], result["problems"]


def test_executor_recovers_after_losing_its_pod():
    kubectl(
        "-n",
        "capacity-execution",
        "delete",
        "pod",
        "-l",
        "app.kubernetes.io/name=capacity-executor",
        "--wait=true",
        timeout=180,
    )

    def available():
        dep = json.loads(kubectl("-n", "capacity-execution", "get", "deployment", "capacity-executor", "-o", "json"))
        return dep["status"].get("availableReplicas", 0) >= 1 and dep["status"].get("unavailableReplicas", 0) == 0

    wait_until(available, timeout=300, what="executor available again")
    from capacity_platform.lab.backup import _psql

    # Reconcile-first start-up: nothing may be left claiming to be mid-write after the restart.
    stuck = _psql(
        "SELECT count(*) FROM execution.operation WHERE state = 'in_progress' "
        "AND updated_at < now() - interval '10 minutes'"
    )
    assert stuck == "0"
