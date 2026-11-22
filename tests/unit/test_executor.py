"""S25 reconcile-first executor and S26 verification/recovery (S25-T01..T14, S26-T01..T18 subset)."""

from datetime import timedelta

import pytest
from tests.fakes import FakeGovernance, FakeKube, MemoryStore, deployment
from tests.unit.test_governance import (
    NOW,
    OPERATOR,
    HumanRole,
    approved,
    decide,
    issued,
)

from capacity_platform.contracts.enums import GateStatus, OperatingMode
from capacity_platform.contracts.ownership import detect_ownership
from capacity_platform.executor.operations import (
    AuthorityBundle,
    Environment,
    Executor,
    FenceLost,
    Operation,
    St,
    build_patch,
    container_index,
)
from capacity_platform.executor.verification import (
    GuardrailThresholds,
    WindowSummary,
    plan_recovery,
    verify_configuration,
    verify_operational,
)
from capacity_platform.governance.plans import PlanState, request_execution

ENV = Environment(cluster_incarnation="inc-test-0001", host_safety="ok", hpa_present=False)
T = NOW + timedelta(minutes=3)


def bundle(**overrides):
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
        idempotency_key="k",
        now=NOW + timedelta(minutes=2),
    )
    data = dict(
        plan=plan,
        plan_state=PlanState.EXECUTION_REQUESTED,
        approval=record,
        approval_revoked=False,
        intent=intent,
        mode=OperatingMode.GOVERNED,
        current_policy_digest=plan.policyDigest,
        requester_grant_valid=True,
    )
    data.update(overrides)
    return AuthorityBundle(**data)


def run(kube, b=None, store=None, env=ENV, when=T):
    store = store or MemoryStore()
    ex = Executor(kube, FakeGovernance(b or bundle()), store, "worker-1")
    return ex.run("intent-1", env, when), store


def test_happy_path_applies_exact_fields_with_preconditions():
    kube = FakeKube(deployment())
    op, _store = run(kube)
    assert op.state == St.APPLIED_PENDING_VERIFICATION
    assert len(kube.writes) == 1 and kube.dry_runs == 1
    patch = kube.writes[0]
    tests = {p["path"] for p in patch if p["op"] == "test"}
    assert {"/metadata/uid", "/metadata/resourceVersion"} <= tests
    mutated = [p["path"] for p in patch if p["op"] != "test"]
    assert all("/resources/" in p for p in mutated)  # never replicas, image, labels, SA...
    # The journal recorded the intent before the external write.
    first_in_progress = next(t for t in op.history if t.to_state == St.IN_PROGRESS)
    assert first_in_progress.reason == "operation_intent_persisted"


def test_t01_forged_intent_digest_causes_no_mutation():
    b = bundle()
    forged = b.intent.model_copy(update={"targetKey": "something-else"})
    kube = FakeKube(deployment())
    op, _ = run(kube, bundle(intent=forged))
    assert op.state == St.REFUSED and op.refusalReason == "intent_integrity_invalid"
    assert kube.writes == []


def test_t02_recreated_controller_with_same_name_is_refused():
    kube = FakeKube(deployment(uid="uid-recreated"))
    op, _ = run(kube)
    assert op.refusalReason == "target_identity_changed" and kube.writes == []


def test_t03_concurrent_edit_between_get_and_patch_never_overwrites():
    kube = FakeKube(deployment())
    kube.bump_before_write = True
    op, _ = run(kube)
    assert kube.writes == []  # the resourceVersion `test` op failed server-side
    assert op.state in (St.REFUSED, St.ESCALATED)


def test_t05_active_hpa_refuses_cpu_request_change():
    kube = FakeKube(deployment())
    op, _ = run(kube, env=ENV.model_copy(update={"hpa_present": True}))
    assert op.refusalReason == "hpa_request_coupling_unmodeled" and kube.writes == []


def test_t06_gitops_owned_target_is_never_direct_patched():
    kube = FakeKube(deployment(labels={"kustomize.toolkit.fluxcd.io/name": "workloads"}))
    op, _ = run(kube)
    assert op.refusalReason == "ownership_gitops_managed" and kube.writes == []


def test_t09_lost_response_after_successful_patch_reconciles_without_duplicate():
    kube = FakeKube(deployment())
    kube.lose_response_after_write = True
    op, store = run(kube)
    assert op.state == St.APPLIED_PENDING_VERIFICATION
    assert len(kube.writes) == 1
    assert any(t.to_state == St.UNKNOWN for t in op.history)
    # Replaying the same intent later must not patch again.
    op2, _ = run(kube, store=store)
    assert len(kube.writes) == 1 and op2.state == St.APPLIED_PENDING_VERIFICATION


def test_t11_crash_after_patch_before_journal_ack_is_reconciled_not_repatched():
    kube = FakeKube(deployment())
    store = MemoryStore()
    # Simulate: worker A journaled IN_PROGRESS, patched, then died before saving the result.
    b = bundle()
    ex = Executor(kube, FakeGovernance(b), store, "worker-a")
    original_save = store.save
    calls = {"n": 0}

    def crashing_save(op):
        calls["n"] += 1
        original_save(op)
        if op.state == St.IN_PROGRESS:
            kube.patch_deployment(b.plan.target.namespace, b.plan.target.controller.name, op.patch, dry_run=False)
            raise SystemExit("worker crashed after the external write")

    store.save = crashing_save
    with pytest.raises(SystemExit):
        ex.run("intent-1", ENV, T)
    store.save = original_save
    op, _ = run(kube, b=b, store=store)  # worker B takes over
    assert op.state == St.APPLIED_PENDING_VERIFICATION
    assert len(kube.writes) == 1


def test_t10_crash_after_intent_before_patch_does_not_blindly_patch():
    kube = FakeKube(deployment())
    store = MemoryStore()
    b = bundle()
    op = Operation(
        operationId="op",
        intentId="intent-1",
        planId="plan-1",
        planRevision=1,
        planDigest=b.plan.planDigest,
        targetKey=b.plan.target.target_key(),
        adapter="direct_kubernetes",
        state=St.IN_PROGRESS,
        observedResourceVersion="100",
        fencingEpoch=0,
    )
    store.ops["intent-1"] = op
    result, _ = run(kube, b=b, store=store)
    assert kube.writes == []
    assert result.state == St.REFUSED and result.refusalReason == "submission_unapplied"


def test_t12_stale_fence_cannot_commit_or_mutate():
    kube = FakeKube(deployment())
    store = MemoryStore()
    ex = Executor(kube, FakeGovernance(bundle()), store, "worker-old")
    original_claim = store.claim

    def claim_then_lose(intent_id, worker_id, now):
        epoch = original_claim(intent_id, worker_id, now)
        store.epoch += 1  # another worker claimed in the meantime
        return epoch

    store.claim = claim_then_lose
    with pytest.raises(FenceLost):
        ex.run("intent-1", ENV, T)
    assert kube.writes == []


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"mode": OperatingMode.ADVISORY}, "mode_not_governed"),
        ({"approval_revoked": True}, "approval_revoked"),
        ({"requester_grant_valid": False}, "requester_grant_revoked"),
        ({"current_policy_digest": "sha256:" + "1" * 64}, "policy_superseded"),
    ],
)
def test_authority_changes_after_approval_are_refused(overrides, reason):
    kube = FakeKube(deployment())
    op, _ = run(kube, bundle(**overrides))
    assert op.state == St.REFUSED and reason in op.reconciliation["refusalReasons"]
    assert kube.writes == []


def test_t14_exclusive_experiment_blocks_new_mutation():
    kube = FakeKube(deployment())
    op, _ = run(kube, env=ENV.model_copy(update={"host_safety": "exclusive"}))
    assert op.refusalReason == "exclusive_experiment" and kube.writes == []


def test_expired_intent_is_refused():
    kube = FakeKube(deployment())
    op, _ = run(kube, when=NOW + timedelta(hours=2))
    assert op.state == St.REFUSED and kube.writes == []


def test_before_image_drift_is_refused():
    kube = FakeKube(deployment(cpu_req="175m"))
    op, _ = run(kube)
    assert op.refusalReason == "before_image_mismatch"


def test_admission_rejection_records_refusal():
    kube = FakeKube(deployment())
    kube.reject = True
    op, _ = run(kube)
    assert op.refusalReason == "dry_run_rejected" and kube.writes == []


def test_ownership_detection_fails_closed_on_conflicting_signals():
    obj = deployment(labels={"capacity.lab/ownership": "direct", "argocd.argoproj.io/instance": "x"})
    assert detect_ownership(obj) == "ambiguous"


def test_absent_limit_is_added_not_assumed():
    obj = deployment()
    del obj["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
    b = bundle()
    fs = b.plan.approvedFieldChanges[0].model_copy(update={"field": "cpu_limit", "before": None, "after": 300})
    patch = build_patch(obj, container_index(obj, "app"), "app", (fs,))
    assert {"op": "add", "path": "/spec/template/spec/containers/0/resources/limits", "value": {}} in patch
    assert patch[-1] == {"op": "add", "path": "/spec/template/spec/containers/0/resources/limits/cpu", "value": "300m"}


# ---------------------------------------------------------------- S26 verification and recovery


def _applied():
    kube = FakeKube(deployment())
    op, _store = run(kube)
    return kube, op, bundle().plan


def test_s26_t01_ack_without_rollout_is_not_verified():
    kube, _op, plan = _applied()
    live = kube.get_deployment("workloads-batch", "cpu-worker")
    live["status"]["observedGeneration"] = 3  # controller has not processed generation 4 yet
    cfg = verify_configuration(plan, live, rollout_started=T, now=T + timedelta(seconds=10), deadline_seconds=300)
    assert cfg.configuration == GateStatus.PASS and cfg.rollout == "incomplete"


def _window(**kw):
    base = dict(
        start=T,
        end=T + timedelta(minutes=10),
        coverage=0.99,
        request_rate=10.0,
        availability=1.0,
        oom_restarts=0,
        queue_growth_per_min=0.0,
        cpu_mean_m=40.0,
        observation_ref="obs",
    )
    base.update(kw)
    return WindowSummary(**base)


THRESH = GuardrailThresholds(max_oom_restarts=0, min_availability=0.99, max_queue_growth_per_min=0.5)


def test_missing_post_window_is_unknown_never_pass():
    v = verify_operational(THRESH, 0.2, _window(), None, {"cpu_m": -75})
    assert v.guardrails == GateStatus.UNKNOWN and v.benefit == "inconclusive"


def test_lower_traffic_after_change_makes_benefit_inconclusive():
    v = verify_operational(THRESH, 0.2, _window(request_rate=10.0), _window(request_rate=5.0), {"cpu_m": -75})
    assert v.guardrails == GateStatus.PASS and v.benefit == "inconclusive"
    assert v.benefitReason == "incomparable_load"


def test_queue_growth_after_cpu_cut_fails_guardrail():
    v = verify_operational(THRESH, 0.2, _window(), _window(queue_growth_per_min=4.0), {"cpu_m": -75})
    assert v.guardrails == GateStatus.FAIL


def test_comparable_safe_reduction_supports_benefit():
    v = verify_operational(THRESH, 0.2, _window(), _window(request_rate=10.5), {"cpu_m": -75})
    assert v.benefit == "supported"


def test_recovery_builds_exact_inverse_only_when_after_image_matches():
    kube, op, plan = _applied()
    op.state = St.VERIFICATION_FAILED
    live = kube.get_deployment("workloads-batch", "cpu-worker")
    decision = plan_recovery(plan, op, live, "inc-test-0001")
    assert decision.action == "apply_inverse"
    kube.patch_deployment("workloads-batch", "cpu-worker", decision.patch, dry_run=False)
    restored = kube.get_deployment("workloads-batch", "cpu-worker")
    assert restored["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] == "150m"


def test_recovery_escalates_on_concurrent_edit_recreation_or_attempt_limit():
    kube, op, plan = _applied()
    op.state = St.VERIFICATION_FAILED
    live = kube.get_deployment("workloads-batch", "cpu-worker")
    live["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "90m"
    assert plan_recovery(plan, op, live, "inc-test-0001").reason == "concurrent_edit_detected"
    recreated = deployment(uid="uid-other")
    assert plan_recovery(plan, op, recreated, "inc-test-0001").reason == "target_identity_changed"
    op.recoveryAttempts = 1
    assert (
        plan_recovery(plan, op, kube.get_deployment("workloads-batch", "cpu-worker"), "inc-test-0001").reason
        == "max_inverse_attempts_reached"
    )


# ------------------------------------------------------------------ S26 window observation
# The verifier reads its guardrails through the real telemetry registry and client. These tests stub
# only Prometheus's HTTP answer, so they catch a query the client refuses (for example a step below
# the registered minimum), which would otherwise turn a guardrail into `unknown` on every change.


def _prometheus_answer(monkeypatch, failing: str | None = None):
    import httpx

    from capacity_platform.capacity.telemetry import registry

    values = {
        "container_cpu_usage_seconds_total": "14.0",
        "kube_deployment_status_replicas_available": "1",
        "kube_pod_container_status_restarts_total": "0",
        "lab_queue_depth": "0",
        "lab_requests_total": "1.0",
    }

    def fake_get(url, params, timeout):
        query, start, end, step = params["query"], float(params["start"]), float(params["end"]), params["step"]
        metric = next((m for m in values if m in query), None)
        if failing and failing in query:
            return httpx.Response(503, request=httpx.Request("GET", url))
        result = []
        if metric is not None:  # W04 exports no latency histogram or error ratio: those stay empty
            points = [[start + i * step, values[metric]] for i in range(int((end - start) // step))]
            result = [{"metric": {}, "values": points}]
        return httpx.Response(200, json={"data": {"result": result}}, request=httpx.Request("GET", url))

    monkeypatch.setattr(registry.httpx, "get", fake_get)
    return registry.PrometheusClient("http://prometheus.test")


def test_healthy_fully_observed_w04_window_passes_its_guardrails(monkeypatch):
    from pathlib import Path

    from capacity_platform.executor.service import observe_window
    from capacity_platform.executor.verification import load_verification_policy

    prom = _prometheus_answer(monkeypatch)
    policy = load_verification_policy(Path("configs/policies/verification-policy.yaml"))

    baseline = observe_window(prom, "w04-cpu-worker", T - timedelta(minutes=15), T)
    post = observe_window(prom, "w04-cpu-worker", T + timedelta(minutes=2), T + timedelta(minutes=17))
    verdict = verify_operational(
        policy.workloads["w04-cpu-worker"], policy.loadComparabilityTolerance, baseline, post, {"memory_bytes": -1}
    )

    assert post is not None and post.oom_restarts == 0 and post.availability == 1.0
    assert verdict.guardrails == GateStatus.PASS, verdict.guardrailFindings
    assert verdict.loadComparability == "sufficient"


def test_failed_guardrail_query_is_unknown_never_pass(monkeypatch):
    from capacity_platform.executor.service import observe_window

    prom = _prometheus_answer(monkeypatch, failing="kube_pod_container_status_restarts_total")

    post = observe_window(prom, "w04-cpu-worker", T, T + timedelta(minutes=15))
    verdict = verify_operational(THRESH, 0.2, _window(), post, {"memory_bytes": -1})

    assert post is not None and post.oom_restarts is None
    assert verdict.guardrails == GateStatus.UNKNOWN and verdict.guardrailFindings["oom_restarts"] == "unknown"
