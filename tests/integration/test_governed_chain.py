"""Critical governed path through real PostgreSQL roles (S02-TEST-004/006/009, S24, S25).

capacity (svc_capacity) persists a qualified evaluation -> governance (svc_governance) issues,
approves and requests execution with three distinct humans -> executor (svc_executor) reads
authority only through the governance view and applies the patch to a fake Kubernetes API.
"""

from datetime import UTC, datetime, timedelta

import pytest
from tests import factories as f
from tests.fakes import FakeKube, deployment
from tests.unit.test_governance import ADMIN, APPROVER, OPERATOR, PLANNER

from capacity_platform.capacity.simulation.evaluate import evaluate
from capacity_platform.capacity.store import CapacityStore
from capacity_platform.contracts.enums import HumanRole, OperatingMode
from capacity_platform.contracts.errors import PlatformError
from capacity_platform.executor.operations import Environment, Executor, St
from capacity_platform.executor.store import SqlExecutionStore, SqlGovernancePort
from capacity_platform.governance.authz import Grant
from capacity_platform.governance.service import GovernanceService
from capacity_platform.governance.store import GovernanceStore
from capacity_platform.shared import jobs
from capacity_platform.shared.auth.oidc import HumanPrincipal
from capacity_platform.shared.db import connect, transaction

pytestmark = pytest.mark.integration
NOW = datetime(2026, 11, 19, 8, 0, tzinfo=UTC)
ENV = Environment(cluster_incarnation="inc-test-0001", host_safety="ok", hpa_present=False)


def human(actor, *roles):
    return HumanPrincipal(actor=actor, roles=frozenset(roles), client_id="capacityctl", expires_at=0)


def seed_evaluation(role_dsn, uid: str) -> str:
    """Persist a qualified W04 evaluation for one controller UID; returns its digest."""
    ev = f.good_evidence("w04-cpu-worker", cpu_p95=40.0, forecast_cpu_upper=45.0)
    analysis = evaluate(f.target_state("w04-cpu-worker", uid=uid), ev, f.policy("w04-cpu-worker"), f.lab_nodes())
    with connect(role_dsn("svc_capacity")) as conn, transaction(conn):
        CapacityStore(conn).save_analysis(analysis, "workloads-batch", "w04-cpu-worker", "corr-seed")
    return analysis.qualified()[0].integrityDigest


@pytest.fixture(scope="module")
def seeded(role_dsn):
    with connect(role_dsn("svc_governance")) as conn, transaction(conn):
        store = GovernanceStore(conn)
        for actor, role, gid in (
            (PLANNER, HumanRole.PLANNER, "g-pl"),
            (APPROVER, HumanRole.APPROVER, "g-ap"),
            (OPERATOR, HumanRole.EXECUTOR, "g-ex"),
            (ADMIN, HumanRole.ADMINISTRATOR, "g-ad"),
            (PLANNER, HumanRole.APPROVER, "g-pl-ap"),
        ):
            store.add_grant(
                Grant(gid, actor.key(), role, "capacity-lab", frozenset({"workloads-batch", "*cluster*"}), 1),
                "bootstrap",
            )
        pol = f.policy("w04-cpu-worker")
        store.set_effective_policy("w04-cpu-worker", pol.policyId, pol.revision, pol.digest, True)
    return lambda uid: seed_evaluation(role_dsn, uid)


@pytest.fixture()
def service(role_dsn):
    clock = {"now": NOW}
    svc = GovernanceService(lambda: connect(role_dsn("svc_governance")), clock=lambda: clock["now"])
    svc.dsn = role_dsn("svc_governance")
    return svc


def governed(service):
    with connect(service.dsn) as conn:
        mode, rev = GovernanceStore(conn).mode()
    if mode != OperatingMode.GOVERNED:
        service.set_mode(human(ADMIN, HumanRole.ADMINISTRATOR), OperatingMode.GOVERNED, rev)


def full_chain(service, seeded, plan_id, uid):
    governed(service)
    created = service.create_plan(human(PLANNER, HumanRole.PLANNER, HumanRole.APPROVER), seeded(uid), plan_id, None)
    assert created["state"] == "AwaitingApproval"
    plan = created["plan"]
    service.decide(human(APPROVER, HumanRole.APPROVER), plan_id, 1, plan["planDigest"], "approve")
    intent = service.request_execution(human(OPERATOR, HumanRole.EXECUTOR), plan_id, 1, f"idem-{plan_id}")
    return plan, intent["intentId"]


def run_executor(role_dsn, intent_id, kube, when):
    with connect(role_dsn("svc_executor")) as conn:
        conn.autocommit = True
        return Executor(kube, SqlGovernancePort(conn), SqlExecutionStore(conn), "executor-0").run(intent_id, ENV, when)


def test_governed_chain_applies_once_and_journals_everything(role_dsn, seeded, service):
    _plan, intent_id = full_chain(service, seeded, "plan-chain-ok", "uid-chain-ok")
    kube = FakeKube(deployment(uid="uid-chain-ok"))
    op = run_executor(role_dsn, intent_id, kube, NOW + timedelta(minutes=1))
    assert op.state == St.APPLIED_PENDING_VERIFICATION and len(kube.writes) == 1
    # The governance handoff is a durable job carrying only the intent ID.
    with connect(role_dsn("svc_executor")) as conn:
        job = jobs.claim(conn, queue="executor", worker="w", lease=timedelta(seconds=30))
        assert job is not None and set(job.payload) == {"intentId"}
        states = [
            r["to_state"]
            for r in conn.execute(
                "SELECT to_state FROM execution.operation_transition WHERE intent_id = %s ORDER BY seq", (intent_id,)
            )
        ]
        assert states.index("in_progress") < states.index("applied_pending_verification")
    # Re-delivery of the same intent never patches twice.
    run_executor(role_dsn, intent_id, kube, NOW + timedelta(minutes=2))
    assert len(kube.writes) == 1


def test_idempotent_execution_request_returns_same_intent(role_dsn, seeded, service):
    governed(service)
    plan = service.create_plan(human(PLANNER, HumanRole.PLANNER), seeded("uid-idem"), "plan-idem", None)["plan"]
    service.decide(human(APPROVER, HumanRole.APPROVER), "plan-idem", 1, plan["planDigest"], "approve")
    who = human(OPERATOR, HumanRole.EXECUTOR)
    first = service.request_execution(who, "plan-idem", 1, "same-key")
    assert service.request_execution(who, "plan-idem", 1, "same-key") == first


def test_author_cannot_approve_even_holding_approver_grant(seeded, service):
    governed(service)
    plan = service.create_plan(human(PLANNER, HumanRole.PLANNER), seeded("uid-sod"), "plan-sod", None)["plan"]
    with pytest.raises(PlatformError) as exc:
        service.decide(human(PLANNER, HumanRole.APPROVER), "plan-sod", 1, plan["planDigest"], "approve")
    assert exc.value.code == "separation_of_duties"


def test_grant_revoked_after_intent_blocks_execution(role_dsn, seeded, service):
    _, intent_id = full_chain(service, seeded, "plan-revoked-grant", "uid-revoked")
    with connect(role_dsn("svc_governance")) as conn, transaction(conn):
        GovernanceStore(conn).revoke_grant("g-ex", "security-admin")
    kube = FakeKube(deployment(uid="uid-revoked"))
    try:
        op = run_executor(role_dsn, intent_id, kube, NOW + timedelta(minutes=1))
        assert op.state == St.REFUSED and "requester_grant_revoked" in op.reconciliation["refusalReasons"]
        assert kube.writes == []
    finally:
        with connect(role_dsn("svc_governance")) as conn, transaction(conn):
            GovernanceStore(conn).add_grant(
                Grant("g-ex-2", OPERATOR.key(), HumanRole.EXECUTOR, "capacity-lab", frozenset({"workloads-batch"}), 2),
                "bootstrap",
            )


def test_mode_downgrade_after_approval_blocks_execution(role_dsn, seeded, service):
    _, intent_id = full_chain(service, seeded, "plan-mode-change", "uid-mode")
    with connect(role_dsn("svc_governance")) as conn:
        _, rev = GovernanceStore(conn).mode()
    service.set_mode(human(ADMIN, HumanRole.ADMINISTRATOR), OperatingMode.ADVISORY, rev)
    kube = FakeKube(deployment(uid="uid-mode"))
    op = run_executor(role_dsn, intent_id, kube, NOW + timedelta(minutes=1))
    assert op.state == St.REFUSED and "mode_not_governed" in op.reconciliation["refusalReasons"]
    assert kube.writes == []


def test_out_of_scope_plan_read_is_indistinguishable_from_missing(service, seeded):
    outsider = human(PLANNER.model_copy(update={"subject": "sub-outsider"}), HumanRole.PLANNER)
    with pytest.raises(PlatformError) as missing:
        service.get_plan(outsider, "plan-does-not-exist", None)
    with pytest.raises(PlatformError) as foreign:
        service.get_plan(outsider, "plan-chain-ok", None)
    assert missing.value.code == foreign.value.code and missing.value.message == foreign.value.message


def test_second_operation_on_same_controller_waits_for_first(role_dsn, seeded, service):
    _, first = full_chain(service, seeded, "plan-serial-a", "uid-serial")
    _, second = full_chain(service, seeded, "plan-serial-b", "uid-serial")
    kube = FakeKube(deployment(uid="uid-serial"))
    assert run_executor(role_dsn, first, kube, NOW + timedelta(minutes=1)).state == St.APPLIED_PENDING_VERIFICATION
    # The first change is still awaiting verification, so the controller stays locked.
    assert run_executor(role_dsn, second, kube, NOW + timedelta(minutes=1)) is None
    assert len(kube.writes) == 1


def test_refused_commands_leave_durable_denial_records(role_dsn, seeded, service):
    # A refusal rolls back the command's transaction; its audit record must survive that rollback.
    governed(service)
    plan = service.create_plan(human(PLANNER, HumanRole.PLANNER), seeded("uid-audit"), "plan-audit", None)["plan"]
    attempts = [
        (human(PLANNER, HumanRole.APPROVER), plan["planDigest"]),  # the author approving own plan
        (human(APPROVER, HumanRole.APPROVER), "sha256:" + "0" * 64),  # a digest the approver never saw
        (human(OPERATOR, HumanRole.EXECUTOR), plan["planDigest"]),  # a caller without the Approver role
    ]
    for who, plan_digest in attempts:
        with pytest.raises(PlatformError):
            service.decide(who, "plan-audit", 1, plan_digest, "approve")

    with connect(role_dsn("svc_auditor")) as conn:
        rows = conn.execute(
            "SELECT action, reason_code, actor_human_ref FROM audit.events "
            "WHERE decision = 'deny' AND resource_scope->>'planId' = 'plan-audit' ORDER BY seq"
        ).fetchall()
    assert [(r["action"], r["reason_code"]) for r in rows[:2]] == [
        ("plan.approve", "separation_of_duties"),
        ("plan.approve", "plan_digest_mismatch"),
    ]
    # The caller only saw a generic denial; the audit keeps the precise authorization reason.
    assert len(rows) == 3 and rows[2]["reason_code"] != "AUTHORIZATION_DENIED"
    assert rows[2]["actor_human_ref"] == OPERATOR.key()
