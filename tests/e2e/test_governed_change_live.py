"""The one critical end-to-end path (S30): a resource change goes from analysis to verified outcome
on the live lab, with four different people and real Keycloak tokens.

planner1 requests an analysis and creates a plan -> planner1 cannot approve it (separation of
duties) -> approver1 approves the exact digest -> operator1 requests execution -> the executor
patches the Deployment -> the live Deployment shows exactly the planned values -> the operation
reaches a verification outcome.

Lower layers already cover each rule in isolation; this test proves the pieces are wired together.
When the engine has no qualified candidate (for example a fresh lab with too little telemetry) the
test is skipped with the engine's own refusal reasons — it never forces a change.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from tests.live import HOME, api, kubectl, require_lab, token, wait_until

from capacity_platform.contracts.units import cpu_to_millicores, memory_to_bytes

pytestmark = pytest.mark.e2e

WORKLOAD = "w04-cpu-worker"
NAMESPACE, DEPLOYMENT = "workloads-batch", "cpu-worker"
TERMINAL = {"verified_success", "verification_failed", "verification_inconclusive", "recovered", "escalated", "refused"}
# The people acting. Every call asks `token()` for that person's current token, because the test
# outlives a single access token while it waits for verification.
PLANNER, APPROVER, OPERATOR, ADMIN = "planner1", "approver1", "operator1", "admin1"


@pytest.fixture(scope="module", autouse=True)
def lab():
    require_lab()


def _set_mode(mode: str) -> None:
    current = api("GET", "/api/v1/capabilities", token(ADMIN)).json()
    if current["mode"] == mode:
        return
    body = {"mode": mode, "expectedRevision": current["modeRevision"]}
    resp = api("PUT", "/api/v1/mode", token(ADMIN), json=body)
    assert resp.status_code == 200, resp.text


def _analysis() -> dict:
    created = api(
        "POST",
        "/api/v1/analyses",
        token(PLANNER),
        json={"workloadId": WORKLOAD, "horizon": "1h"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert created.status_code == 202, created.text
    op = created.json()["operationId"]
    done = wait_until(
        lambda: (
            (s := api("GET", f"/api/v1/operations/{op}", token(PLANNER)).json())["state"] not in ("queued", "leased")
            and s
        ),
        timeout=600,
        what="analysis job",
    )
    assert done["state"] == "completed", done
    return done["result"]


@pytest.mark.timeout(3600)
def test_governed_change_from_analysis_to_verified_outcome():
    result = _analysis()
    if not result["qualified"]:
        detail = api("GET", f"/api/v1/analyses/{result['analysisDigest']}", token(PLANNER)).json()
        # Name every gate that stopped each candidate, so the skip explains itself.
        blocked = [
            f"{[f['field'] for f in e['candidate']['fields']]}: "
            + ", ".join(
                f"{g['ruleId']}={g['reasonCode']}" for g in e["hardGates"] if g["status"] in ("fail", "unknown")
            )
            for e in detail.get("evaluations", [])
        ]
        pytest.skip(f"no qualified candidate yet: refusals={detail.get('refusals')} candidates={blocked}")

    evaluation = result["qualified"][0]
    # A plan issued outside Governed mode is advisory only by design, so the mode is set first.
    _set_mode("Governed")
    try:
        plan = api("POST", "/api/v1/plans", token(PLANNER), json={"evaluationDigest": evaluation})
        assert plan.status_code == 201, plan.text
        created, state = plan.json()["plan"], plan.json()["state"]
        plan_id, revision, plan_digest = created["planId"], created["revision"], created["planDigest"]
        assert state == "AwaitingApproval", f"plan not approvable: state={state} {created['risk']}"

        approvals = f"/api/v1/plans/{plan_id}/approvals"
        decision = {"revision": revision, "planDigest": plan_digest, "decision": "approve"}
        # Separation of duties: the author cannot approve, and a wrong digest is refused.
        assert api("POST", approvals, token(PLANNER), json=decision).status_code == 403
        wrong = {**decision, "planDigest": "sha256:" + "0" * 64}
        assert api("POST", approvals, token(APPROVER), json=wrong).status_code in (409, 422)
        approved = api("POST", approvals, token(APPROVER), json=decision)
        assert approved.status_code == 201, approved.text

        requested = api(
            "POST",
            f"/api/v1/plans/{plan_id}/execution-requests",
            token(OPERATOR),
            json={"revision": revision},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert requested.status_code == 202, requested.text
        intent = requested.json()["intentId"]

        def execution() -> dict:
            resp = api("GET", f"/api/v1/executions/{intent}", token(OPERATOR))
            assert resp.status_code == 200, f"execution read failed: {resp.status_code} {resp.text[:200]}"
            return resp.json()

        applied = wait_until(
            lambda: (
                (s := execution()["state"]) in TERMINAL | {"applied_pending_verification", "verification_in_progress"}
                and s
            ),
            timeout=600,
            what="executor to apply the change",
        )
        assert applied != "refused", execution()

        # The live object carries exactly the planned values.
        planned = {c["field"]: c["after"] for c in created["approvedFieldChanges"]}
        live = json.loads(kubectl("-n", NAMESPACE, "get", "deployment", DEPLOYMENT, "-o", "json"))
        resources = live["spec"]["template"]["spec"]["containers"][0]["resources"]
        live_values = {
            "cpu_request": cpu_to_millicores(resources["requests"]["cpu"]),
            "cpu_limit": cpu_to_millicores(resources["limits"]["cpu"]),
            "memory_request": memory_to_bytes(resources["requests"]["memory"]),
            "memory_limit": memory_to_bytes(resources["limits"]["memory"]),
        }
        assert {f: live_values[f] for f in planned} == planned

        final = wait_until(
            lambda: (s := execution()["state"]) in TERMINAL and s,
            timeout=2700,
            interval=30,
            what="verification outcome",
        )
        # Keep the whole record as evidence: plan, approval, operation transitions and outcome.
        record = {
            "analysis": result,
            "plan": api("GET", f"/api/v1/plans/{plan_id}?revision={revision}", token(PLANNER)).json(),
            "execution": execution(),
            "liveResourcesAfterApply": resources,
        }
        out = HOME / "evidence" / f"e2e-governed-change-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
        out.write_text(json.dumps(record, indent=2, default=str))
        assert final in {"verified_success", "verification_inconclusive", "recovered"}, final
    finally:
        _set_mode("Observe")
