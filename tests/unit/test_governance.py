"""S06 authorization and S24 plan/approval/intent governance (S24-T01..T12, S06-TEST-004..013)."""

from datetime import UTC, datetime, timedelta

import pytest
from tests import factories as f

from capacity_platform.capacity.simulation.evaluate import evaluate
from capacity_platform.contracts.enums import HumanRole, OperatingMode
from capacity_platform.contracts.errors import PlatformError
from capacity_platform.contracts.identity import ActorRef
from capacity_platform.governance.authz import Grant, authorize
from capacity_platform.governance.plans import (
    PlanState,
    approval_effective,
    approve,
    check_transition,
    issue_plan,
    request_execution,
)

NOW = datetime(2026, 11, 19, 8, 0, tzinfo=UTC)
ISSUER = "https://auth.capacity.test/realms/capacity"
PLANNER = ActorRef(issuer=ISSUER, subject="sub-planner")
APPROVER = ActorRef(issuer=ISSUER, subject="sub-approver")
OPERATOR = ActorRef(issuer=ISSUER, subject="sub-operator")
ADMIN = ActorRef(issuer=ISSUER, subject="sub-admin")
AGENT = ActorRef(issuer="capacity-ai", subject="RiskReviewer", kind="machine")
NS = "workloads-batch"


def grant(actor, role, namespaces=(NS,), revoked=None, gid=None):
    return Grant(
        grant_id=gid or f"{actor.subject}-{role}",
        subject_key=actor.key(),
        role=role,
        cluster_id="capacity-lab",
        namespaces=frozenset(namespaces),
        revision=1,
        revoked_at=revoked,
    )


GRANTS = [
    grant(PLANNER, HumanRole.PLANNER),
    grant(APPROVER, HumanRole.APPROVER),
    grant(OPERATOR, HumanRole.EXECUTOR),
    grant(ADMIN, HumanRole.ADMINISTRATOR),
    grant(PLANNER, HumanRole.APPROVER, gid="planner-also-approver"),
]


def decide(actor, roles, action, mode=OperatingMode.GOVERNED, grants=GRANTS, namespace=NS):
    return authorize(actor, set(roles), grants, action, cluster_id="capacity-lab", namespace=namespace, mode=mode)


def qualified_evaluation():
    ev = f.good_evidence("w04-cpu-worker", cpu_p95=40.0, forecast_cpu_upper=45.0)
    result = evaluate(f.target_state("w04-cpu-worker"), ev, f.policy("w04-cpu-worker"), f.lab_nodes())
    return result.qualified()[0]


def issued(mode=OperatingMode.GOVERNED, evaluation=None, det_allowed=True, ownership="direct_managed"):
    evaluation = evaluation or qualified_evaluation()
    return issue_plan(
        plan_id="plan-1",
        revision=1,
        author=PLANNER,
        author_decision=decide(PLANNER, {HumanRole.PLANNER}, "plan.create"),
        evaluation=evaluation,
        current_policy_digest=evaluation.policyDigest,
        ownership=ownership,
        mode=mode,
        advisory_status="deterministic_only",
        advisory_ref=None,
        deterministic_only_execution_allowed=det_allowed,
        verification_policy_ref="verification-policy-v1",
        workload_kind="Deployment",
        criticality="Flexible",
        resource_version="100",
        now=NOW,
    )


def approved(plan, state):
    return approve(
        plan=plan,
        state=state,
        approver=APPROVER,
        approver_decision=decide(APPROVER, {HumanRole.APPROVER}, "plan.approve"),
        requested_revision=plan.revision,
        requested_digest=plan.planDigest,
        approval_id="appr-1",
        mode=OperatingMode.GOVERNED,
        now=NOW + timedelta(minutes=1),
    )


# ---------------------------------------------------------------- S06 authorization


def test_viewer_cannot_create_or_approve():
    viewer = ActorRef(issuer=ISSUER, subject="sub-viewer")
    grants = [grant(viewer, HumanRole.VIEWER)]
    assert not decide(viewer, {HumanRole.VIEWER}, "plan.create", grants=grants).allow
    assert not decide(viewer, {HumanRole.VIEWER}, "plan.approve", grants=grants).allow
    assert decide(viewer, {HumanRole.VIEWER}, "inventory.read", grants=grants).allow


def test_approver_outside_namespace_scope_is_denied():
    assert not decide(APPROVER, {HumanRole.APPROVER}, "plan.approve", namespace="workloads-critical").allow


def test_revoked_grant_denies_despite_valid_token_role():
    grants = [grant(APPROVER, HumanRole.APPROVER, revoked=NOW)]
    assert decide(APPROVER, {HumanRole.APPROVER}, "plan.approve", grants=grants).reason == "authorization_denied"


def test_administrator_is_not_an_implicit_approver_or_executor():
    for action in ("plan.approve", "execution.request"):
        assert not decide(ADMIN, {HumanRole.ADMINISTRATOR}, action).allow


def test_machine_principal_cannot_hold_sensitive_actions():
    machine_grants = [grant(AGENT, HumanRole.APPROVER)]
    assert (
        decide(AGENT, {HumanRole.APPROVER}, "plan.approve", grants=machine_grants).reason
        == "machine_principal_not_permitted"
    )


def test_approval_and_execution_denied_outside_governed_mode():
    for mode in (OperatingMode.OBSERVE, OperatingMode.ADVISORY):
        assert decide(APPROVER, {HumanRole.APPROVER}, "plan.approve", mode=mode).reason == "mode_not_governed"
        assert decide(OPERATOR, {HumanRole.EXECUTOR}, "execution.request", mode=mode).reason == "mode_not_governed"


def test_degraded_grant_store_fails_closed_for_sensitive_actions():
    d = authorize(
        APPROVER,
        {HumanRole.APPROVER},
        GRANTS,
        "plan.approve",
        cluster_id="capacity-lab",
        namespace=NS,
        mode=OperatingMode.GOVERNED,
        grant_store_healthy=False,
    )
    assert d.reason == "authorization_freshness_unavailable"


# ---------------------------------------------------------------- S24 plan lifecycle


def test_qualified_evaluation_issues_awaiting_approval_plan():
    plan, state = issued()
    assert state == PlanState.AWAITING_APPROVAL
    assert plan.risk.allRequiredHardGatesPassed
    assert plan.planDigest.startswith("sha256:")
    # Inverse is the exact mirror of the forward change and part of what gets approved.
    assert [(i.before, i.after) for i in plan.inverseFieldChanges] == [
        (c.after, c.before) for c in plan.approvedFieldChanges
    ]


def test_t01_author_cannot_approve_own_plan_even_with_approver_grant():
    plan, state = issued()
    with pytest.raises(PlatformError) as exc:
        approve(
            plan=plan,
            state=state,
            approver=PLANNER,
            approver_decision=decide(PLANNER, {HumanRole.APPROVER}, "plan.approve"),
            requested_revision=1,
            requested_digest=plan.planDigest,
            approval_id="a",
            mode=OperatingMode.GOVERNED,
            now=NOW,
        )
    assert exc.value.code == "separation_of_duties"


def test_t03_changed_payload_does_not_match_approval():
    plan, state = issued()
    with pytest.raises(PlatformError) as exc:
        approve(
            plan=plan,
            state=state,
            approver=APPROVER,
            approver_decision=decide(APPROVER, {HumanRole.APPROVER}, "plan.approve"),
            requested_revision=1,
            requested_digest="sha256:" + "0" * 64,
            approval_id="a",
            mode=OperatingMode.GOVERNED,
            now=NOW,
        )
    assert exc.value.code == "plan_digest_mismatch"
    record = approved(plan, state)
    tampered = plan.model_copy(
        update={"approvedFieldChanges": (plan.approvedFieldChanges[0].model_copy(update={"after": 80}),)}
    ).finalize()
    assert approval_effective(record, tampered, revoked=False, now=NOW) == "plan_digest_mismatch"


def test_t05_expired_or_revoked_approval_cannot_create_intent():
    plan, state = issued()
    record = approved(plan, state)
    ok = decide(OPERATOR, {HumanRole.EXECUTOR}, "execution.request")
    for revoked, when, code in (
        (True, NOW, "approval_revoked"),
        (False, NOW + timedelta(minutes=30), "approval_expired"),
    ):
        with pytest.raises(PlatformError) as exc:
            request_execution(
                plan=plan,
                state=PlanState.APPROVED,
                approval=record,
                approval_revoked=revoked,
                requester=OPERATOR,
                requester_decision=ok,
                mode=OperatingMode.GOVERNED,
                intent_id="i",
                idempotency_key="k",
                now=when,
            )
        assert exc.value.code == code


def test_t06_unknown_mandatory_gate_blocks_plan():
    ev = f.good_evidence("w04-cpu-worker", cpu_p95=40.0, host_safety="unknown")
    result = evaluate(f.target_state("w04-cpu-worker"), ev, f.policy("w04-cpu-worker"), f.lab_nodes())
    indeterminate = next(e for e in result.evaluations if e.status.value == "indeterminate")
    plan, state = issued(evaluation=indeterminate)
    assert state == PlanState.BLOCKED
    with pytest.raises(PlatformError):
        approved(plan, PlanState.AWAITING_APPROVAL)


def test_deterministic_only_execution_needs_explicit_policy():
    _, state = issued(det_allowed=None)
    assert state == PlanState.ADVISORY_ONLY


def test_gitops_owned_target_is_not_executable_in_p0():
    plan, state = issued(ownership="gitops_managed")
    assert plan.adapter == "gitops" and state == PlanState.ADVISORY_ONLY
    assert "adapter_unqualified" in plan.risk.governanceReasons


def test_advisory_mode_plan_is_not_actionable():
    _, state = issued(mode=OperatingMode.ADVISORY)
    assert state == PlanState.ADVISORY_ONLY


def test_t10_execution_request_refused_after_mode_change():
    plan, state = issued()
    record = approved(plan, state)
    with pytest.raises(PlatformError) as exc:
        request_execution(
            plan=plan,
            state=PlanState.APPROVED,
            approval=record,
            approval_revoked=False,
            requester=OPERATOR,
            requester_decision=decide(OPERATOR, {HumanRole.EXECUTOR}, "execution.request"),
            mode=OperatingMode.ADVISORY,
            intent_id="i",
            idempotency_key="k",
            now=NOW,
        )
    assert exc.value.code == "mode_not_governed"


def test_valid_chain_creates_digest_bound_intent():
    plan, state = issued()
    record = approved(plan, state)
    intent = request_execution(
        plan=plan,
        state=PlanState.APPROVED,
        approval=record,
        approval_revoked=False,
        requester=OPERATOR,
        requester_decision=decide(OPERATOR, {HumanRole.EXECUTOR}, "execution.request"),
        mode=OperatingMode.GOVERNED,
        intent_id="intent-1",
        idempotency_key="k1",
        now=NOW + timedelta(minutes=2),
    )
    assert intent.planDigest == plan.planDigest and intent.approvalDigest == record.approvalDigest
    assert intent.expiresAtUtc <= record.expiresAtUtc


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (PlanState.DRAFT, PlanState.APPROVED),
        (PlanState.REJECTED, PlanState.APPROVED),
        (PlanState.APPROVED, PlanState.CONSUMED_BY_EXECUTOR),
    ],
)
def test_illegal_plan_transitions_are_rejected(src, dst):
    with pytest.raises(PlatformError):
        check_transition(src, dst)
