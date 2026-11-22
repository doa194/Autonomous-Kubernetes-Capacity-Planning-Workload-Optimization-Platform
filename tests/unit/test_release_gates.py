"""S32 release-gate rules: missing evidence never passes, hard failures block, reviewers cannot override."""

from capacity_platform.release.gates import (
    GATES,
    EvidenceItem,
    ItemStatus,
    Verdict,
    apply_review,
    evaluate_gate,
    junit_items,
    overall,
)


def _passing_gates():
    return [evaluate_gate(g, [EvidenceItem("x", ItemStatus.PASSED)]) for g in GATES]


def test_gate_without_any_evidence_is_not_qualified():
    assert evaluate_gate(GATES[0], []).verdict is Verdict.NOT_QUALIFIED


def test_hard_failure_blocks_even_when_other_items_pass():
    result = evaluate_gate(
        GATES[1], [EvidenceItem("unit", ItemStatus.PASSED), EvidenceItem("authz", ItemStatus.FAILED, hard=True)]
    )
    assert result.verdict is Verdict.BLOCKED


def test_soft_failure_or_not_run_is_not_qualified_rather_than_blocked():
    result = evaluate_gate(
        GATES[2], [EvidenceItem("b1", ItemStatus.NOT_RUN), EvidenceItem("docs", ItemStatus.FAILED, hard=False)]
    )
    assert result.verdict is Verdict.NOT_QUALIFIED
    assert "not_run:b1" in result.reasons


def test_release_is_blocked_by_any_blocked_gate_and_needs_all_six_gates():
    gates = _passing_gates()
    assert overall(gates) == "QUALIFIED_PENDING_HUMAN_REVIEW"
    assert overall(gates[:5]) == "NOT_QUALIFIED"  # a gate that was never computed is not a pass
    gates[3] = evaluate_gate(GATES[3], [EvidenceItem("restore", ItemStatus.FAILED, hard=True)])
    assert overall(gates) == "BLOCKED"


def test_reviewer_can_refuse_a_pass_but_cannot_override_a_failure():
    assert apply_review("QUALIFIED_PENDING_HUMAN_REVIEW", True) == "QUALIFIED"
    assert apply_review("QUALIFIED_PENDING_HUMAN_REVIEW", False) == "REFUSED_BY_REVIEWER"
    assert apply_review("BLOCKED", True) == "BLOCKED"
    assert apply_review("NOT_QUALIFIED", True) == "NOT_QUALIFIED"


def test_junit_results_map_missing_skipped_and_failed_runs_honestly():
    assert junit_items("e2e", None, hard=True)[0].status is ItemStatus.NOT_RUN
    assert junit_items("e2e", {"tests": 3, "skipped": 3}, hard=True)[0].status is ItemStatus.NOT_RUN
    assert junit_items("e2e", {"tests": 3, "skipped": 1}, hard=True)[0].status is ItemStatus.UNQUALIFIED
    failed = junit_items("security", {"tests": 9, "failures": 1}, hard=True)[0]
    assert failed.status is ItemStatus.FAILED and failed.hard
    assert junit_items("unit", {"tests": 9}, hard=False)[0].status is ItemStatus.PASSED
