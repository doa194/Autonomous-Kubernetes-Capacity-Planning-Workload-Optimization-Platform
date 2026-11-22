"""Domain E golden fixtures (companion §9) plus S19 policy-merge and gate-oracle properties.

These fixtures prove implementation invariants of the deterministic optimizer. They say
nothing about how accurate the scheduler approximation is on a real cluster.
"""

import pytest
from tests import factories as f

from capacity_platform.capacity.candidates import ResourceCandidate
from capacity_platform.capacity.placement.fit import PodShape, fit_single, fragmentation, place_many
from capacity_platform.capacity.policy.gates import GateResult, classify
from capacity_platform.capacity.policy.model import PolicyConflict, resolve_effective_policy
from capacity_platform.capacity.simulation.evaluate import (
    Objectives,
    OptimizationEvaluation,
    PlacementSummary,
    evaluate,
    rank_qualified,
)
from capacity_platform.contracts.enums import Criticality, Eligibility, GateStatus, ResourceField
from capacity_platform.contracts.units import MIB


def _gate(evaluation, rule):
    return next(g for g in evaluation.hardGates if g.ruleId == rule)


def test_f01_split_fit_aggregate_is_not_node_fit():
    nodes = [
        f.node("a", cpu_free_m=600, mem_free_bytes=300 * MIB),
        f.node("b", cpu_free_m=200, mem_free_bytes=900 * MIB),
    ]
    shape = PodShape(cpu_m=400, memory_bytes=500 * MIB, node_selector=dict(f.EXPERIMENT_SELECTOR))
    assert fit_single(nodes, shape).status == "infeasible"
    frag = fragmentation(nodes, shape)
    assert frag.aggregate_would_suggest_fit is True
    assert frag.fit_capable_nodes == 0
    assert frag.stranded_cpu_m_for_shape == 800


def test_f02_memory_gap_across_burst_blocks_safe_downsize():
    ev = f.good_evidence("w03-memory-profile", mem_max=120 * MIB).model_copy(
        update={
            "memory": f.good_evidence("w03-memory-profile").memory.model_copy(
                update={
                    "max_sampled_bytes": 120 * MIB,
                    "p99_bytes": 110 * MIB,
                    "max_gap_seconds": 300,
                    "flags": ["scrape_gap"],
                }
            )
        }
    )
    result = evaluate(f.target_state("w03-memory-profile"), ev, f.policy("w03-memory-profile"), f.lab_nodes())
    mem_downsizes = [
        e
        for e in result.evaluations
        if any(c.field == ResourceField.MEMORY_REQUEST and c.is_decrease for c in e.candidate.fields)
    ]
    assert mem_downsizes, "the generator should still show the candidate, with its reason"
    for e in mem_downsizes:
        assert e.status != Eligibility.QUALIFIED
        assert _gate(e, "memory.oom_spike").status == GateStatus.UNKNOWN


def test_f03_latency_guard_blocks_cpu_downsize_despite_low_cpu():
    ev = f.good_evidence("w01-latency-api", cpu_p95=40.0, slo_p95=210.0)  # p95 above the 150 ms target
    result = evaluate(f.target_state("w01-latency-api"), ev, f.policy("w01-latency-api"), f.lab_nodes())
    cpu_downs = [e for e in result.evaluations if e.candidate.reservation_delta()["cpu_m"] < 0]
    assert cpu_downs
    for e in cpu_downs:
        assert e.status == Eligibility.REJECTED
        assert _gate(e, "slo.latency_errors").reasonCode == "slo_violation"
        assert e.rank is None


def test_f03_missing_latency_evidence_is_unknown_not_pass():
    ev = f.good_evidence("w01-latency-api", cpu_p95=40.0, slo=None)
    result = evaluate(f.target_state("w01-latency-api"), ev, f.policy("w01-latency-api"), f.lab_nodes())
    for e in result.evaluations:
        if e.candidate.reservation_delta()["cpu_m"] < 0:
            assert e.status == Eligibility.INDETERMINATE


def test_f04_low_cpu_with_growing_queue_is_not_qualified():
    ev = f.good_evidence("w04-cpu-worker", cpu_p95=40.0)
    ev = ev.model_copy(update={"queue": ev.queue.model_copy(update={"queue_depth_trend_per_min": 3.0})})
    result = evaluate(f.target_state("w04-cpu-worker"), ev, f.policy("w04-cpu-worker"), f.lab_nodes())
    for e in result.evaluations:
        if e.candidate.reservation_delta()["cpu_m"] < 0:
            assert e.status == Eligibility.REJECTED
            assert _gate(e, "slo.queue_completion").reasonCode == "batch_deadline_risk"


def test_f05_job_template_is_advisory_only_for_governed_execution():
    ev = f.good_evidence("w05-analytics-processor")
    result = evaluate(f.target_state("w05-analytics-processor"), ev, f.policy("w05-analytics-processor"), f.lab_nodes())
    assert result.status == "rejected_before_generation"
    assert any(g.reasonCode == "action_unsupported" for g in result.baselineGates)
    advisory = evaluate(
        f.target_state("w05-analytics-processor"),
        ev,
        f.policy("w05-analytics-processor"),
        f.lab_nodes(),
        intended_use="advisory",
    )
    assert advisory.status == "completed"


def test_f06_active_hpa_blocks_cpu_request_change():
    ev = f.good_evidence("w02-burst-api", cpu_p95=40.0, hpa=True)
    result = evaluate(f.target_state("w02-burst-api"), ev, f.policy("w02-burst-api"), f.lab_nodes())
    cpu_changes = [e for e in result.evaluations if ResourceField.CPU_REQUEST in e.candidate.changes()]
    assert cpu_changes
    for e in cpu_changes:
        assert e.status != Eligibility.QUALIFIED
        assert _gate(e, "hpa.compatibility").reasonCode == "hpa_request_coupling_unmodeled"


def test_f07_platform_tainted_node_never_used_for_demo_pods():
    nodes = [
        f.node(
            "capacity-lab-worker",
            cpu_free_m=4000,
            mem_free_bytes=4000 * MIB,
            pool="platform",
            taints=(("capacity.lab/dedicated", "platform", "NoSchedule"),),
        ),
        f.node("capacity-lab-worker2", cpu_free_m=50, mem_free_bytes=50 * MIB),
    ]
    shape = PodShape(cpu_m=200, memory_bytes=256 * MIB, node_selector=dict(f.EXPERIMENT_SELECTOR))
    result = fit_single(nodes, shape)
    assert result.status == "infeasible"
    platform = next(n for n in result.per_node if n.node == "capacity-lab-worker")
    assert "node_selector_mismatch" in platform.reasons and "taint_not_tolerated" in platform.reasons


def _synthetic_eval(digest_seed: str, status: Eligibility, efficiency: float) -> OptimizationEvaluation:
    cand = f.target_state("w04-cpu-worker")  # only to borrow a target ref
    candidate = ResourceCandidate.model_construct(
        candidateDigest=f"sha256:{digest_seed * 64}"[:71], fields=(), target=cand.target
    )
    return OptimizationEvaluation.model_construct(
        candidate=candidate,
        policyDigest="p",
        status=status,
        hardGates=[],
        placement=PlacementSummary(
            status="feasible",
            fitCapableNodes=1,
            perNodeReasons={},
            unknownConstraints=[],
            surgeStatus=None,
            stranded_cpu_m=0,
            stranded_memory_bytes=0,
        ),
        reservationDeltaPerPod={},
        benefitKinds=[],
        objectives=Objectives(efficiency=efficiency, fit_benefit=0, risk=0, change_cost=0.25),
        paretoFront=None,
        score=None,
        rank=None,
        empiricalValidation="not_performed",
        integrityDigest="",
    )


def test_f08_only_qualified_candidates_are_ranked():
    c1 = _synthetic_eval("1", Eligibility.QUALIFIED, 0.15)
    c2 = _synthetic_eval("2", Eligibility.INDETERMINATE, 0.35)
    c3 = _synthetic_eval("3", Eligibility.REJECTED, 0.25)
    ranked = rank_qualified([c1, c2, c3], f.policy("w04-cpu-worker"))
    assert [r.candidate.candidateDigest for r in ranked] == [c1.candidate.candidateDigest]
    assert ranked[0].rank == 1


def test_f09_recreated_controller_changes_candidate_identity():
    ev = f.good_evidence("w04-cpu-worker", cpu_p95=40.0)
    a = evaluate(f.target_state("w04-cpu-worker", uid="uid-old"), ev, f.policy("w04-cpu-worker"), f.lab_nodes())
    b = evaluate(f.target_state("w04-cpu-worker", uid="uid-new"), ev, f.policy("w04-cpu-worker"), f.lab_nodes())
    assert {e.candidate.candidateDigest for e in a.evaluations}.isdisjoint(
        {e.candidate.candidateDigest for e in b.evaluations}
    )


def test_f10_unsupported_scheduler_constraint_is_indeterminate():
    target = f.target_state("w04-cpu-worker", unsupported=("pod_anti_affinity_required",))
    result = evaluate(
        target, f.good_evidence("w04-cpu-worker", cpu_p95=40.0), f.policy("w04-cpu-worker"), f.lab_nodes()
    )
    assert result.evaluations
    for e in result.evaluations:
        assert e.status == Eligibility.INDETERMINATE
        assert _gate(e, "scheduler.fit").reasonCode == "scheduler_unsupported"


def test_f11_same_inputs_give_same_digests_and_order():
    ev = f.good_evidence("w04-cpu-worker", cpu_p95=40.0)
    first = evaluate(f.target_state("w04-cpu-worker"), ev, f.policy("w04-cpu-worker"), f.lab_nodes())
    second = evaluate(f.target_state("w04-cpu-worker"), ev, f.policy("w04-cpu-worker"), list(reversed(f.lab_nodes())))
    assert first.analysisDigest == second.analysisDigest
    assert [e.candidate.candidateDigest for e in first.evaluations] == [
        e.candidate.candidateDigest for e in second.evaluations
    ]


def test_f15_oom_in_window_rejects_memory_downsize():
    ev = f.good_evidence("w03-memory-profile", mem_max=120 * MIB)
    ev = ev.model_copy(update={"memory": ev.memory.model_copy(update={"oom_events": 1})})
    result = evaluate(f.target_state("w03-memory-profile"), ev, f.policy("w03-memory-profile"), f.lab_nodes())
    for e in result.evaluations:
        if any(c.field.value.startswith("memory") and c.is_decrease for c in e.candidate.fields):
            assert _gate(e, "memory.oom_spike").reasonCode == "oom_risk"
            assert e.status == Eligibility.REJECTED


def test_well_evidenced_cpu_downsize_qualifies_and_is_ranked():
    ev = f.good_evidence("w04-cpu-worker", cpu_p95=40.0, forecast_cpu_upper=45.0)
    result = evaluate(f.target_state("w04-cpu-worker"), ev, f.policy("w04-cpu-worker"), f.lab_nodes())
    best = result.qualified()[0]
    assert best.rank == 1
    assert best.candidate.reservation_delta()["cpu_m"] < 0
    # Benefit is a reservation delta, never a claimed realized saving.
    assert "verified_realized_delta" not in best.benefitKinds


def test_platform_namespace_is_rejected_before_generation():
    target = f.target_state("w04-cpu-worker")
    target = target.model_copy(update={"target": target.target.model_copy(update={"namespace": "capacity-core"})})
    result = evaluate(target, f.good_evidence("w04-cpu-worker"), f.policy("w04-cpu-worker"), f.lab_nodes())
    assert result.status == "rejected_before_generation"
    assert result.evaluations == []


def test_greedy_failure_does_not_prove_infeasibility():
    # Two replicas: a naive "largest node first" pass could strand the second Pod; backtracking finds a valid
    # assignment, and running out of budget yields "not found", never "proven infeasible".
    nodes = [
        f.node("a", cpu_free_m=500, mem_free_bytes=600 * MIB),
        f.node("b", cpu_free_m=300, mem_free_bytes=300 * MIB),
    ]
    shapes = [
        PodShape(cpu_m=300, memory_bytes=300 * MIB, node_selector=dict(f.EXPERIMENT_SELECTOR)),
        PodShape(cpu_m=200, memory_bytes=300 * MIB, node_selector=dict(f.EXPERIMENT_SELECTOR)),
    ]
    assert place_many(nodes, shapes).status == "feasible_under_heuristic"
    assert place_many(nodes, shapes, branch_budget=1).status == "not_found_under_heuristic"


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([GateStatus.PASS, GateStatus.PASS], Eligibility.QUALIFIED),
        ([GateStatus.PASS, GateStatus.NOT_APPLICABLE], Eligibility.QUALIFIED),
        ([GateStatus.PASS, GateStatus.UNKNOWN], Eligibility.INDETERMINATE),
        ([GateStatus.UNKNOWN, GateStatus.FAIL], Eligibility.REJECTED),
    ],
)
def test_four_way_gate_oracle(statuses, expected):
    results = [GateResult(ruleId=f"r{i}", phase="candidate", status=s, reasonCode="x") for i, s in enumerate(statuses)]
    assert classify(results) == expected


def test_workload_override_may_not_relax_class_floor():
    doc = f.POLICY_DOC.model_copy(update={"workloadOverrides": {"w01-latency-api": {"cpu_request_floor_m": 10}}})
    with pytest.raises(PolicyConflict):
        resolve_effective_policy(
            doc,
            cluster_id="capacity-lab",
            namespace="workloads-critical",
            workload_id="w01-latency-api",
            criticality=Criticality.CRITICAL,
        )


def test_override_may_tighten_and_narrow_actions():
    doc = f.POLICY_DOC.model_copy(
        update={
            "workloadOverrides": {"w01-latency-api": {"cpu_request_floor_m": 150, "allowedActions": ["cpu_request"]}}
        }
    )
    eff = resolve_effective_policy(
        doc,
        cluster_id="capacity-lab",
        namespace="workloads-critical",
        workload_id="w01-latency-api",
        criticality=Criticality.CRITICAL,
    )
    assert eff.params.cpu_request_floor_m == 150
    assert eff.allowedActions == (ResourceField.CPU_REQUEST,)
    assert eff.digest != f.policy("w01-latency-api").digest
