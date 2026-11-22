"""S32 release-gate rules as pure functions (S32 §2-§3).

Why this file exists: the release decision must be computed the same way every time from
recorded evidence, and it must never turn missing evidence into a pass. Keeping the rules
pure (no I/O) lets unit tests pin them down.

Verdict vocabulary, strongest first:
* `blocked`       - a hard failure (failed auth/safety test, failed restore, tampered pins).
* `not_qualified` - required evidence is missing, not run, or below its threshold.
* `passed`        - every required item is present and passed.

The overall outcome follows the S32 decision diagram: any blocked gate blocks the release; any
gate that is not passed means NOT QUALIFIED; only when all six gates pass can the result be
QUALIFIED, and even then it stays `pending_human_review`. A human reviewer may refuse a passing
candidate but can never override a failed gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

GATES = (
    "RG01-architecture-traceability",
    "RG02-tests-security",
    "RG03-quantitative-evaluation",
    "RG04-operational-reliability",
    "RG05-delivery-compatibility",
    "RG06-docs-handoff",
)


class Verdict(StrEnum):
    PASSED = "passed"
    NOT_QUALIFIED = "not_qualified"
    BLOCKED = "blocked"


class ItemStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"  # evidence absent or the check could not execute
    UNQUALIFIED = "unqualified"  # ran, but the capability is not qualified (e.g. no paired experiments)


@dataclass(frozen=True)
class EvidenceItem:
    item_id: str
    status: ItemStatus
    hard: bool = False  # a FAILED hard item blocks (auth, safety, restore, integrity)
    detail: str = ""


@dataclass(frozen=True)
class GateResult:
    gate: str
    verdict: Verdict
    items: tuple[EvidenceItem, ...] = field(default_factory=tuple)
    reasons: tuple[str, ...] = field(default_factory=tuple)


def evaluate_gate(gate: str, items: list[EvidenceItem]) -> GateResult:
    if not items:
        return GateResult(gate, Verdict.NOT_QUALIFIED, (), ("no_evidence_recorded",))
    blocked = [i.item_id for i in items if i.status is ItemStatus.FAILED and i.hard]
    if blocked:
        return GateResult(gate, Verdict.BLOCKED, tuple(items), tuple(f"hard_failure:{b}" for b in blocked))
    weak = [f"{i.status.value}:{i.item_id}" for i in items if i.status is not ItemStatus.PASSED]
    if weak:
        return GateResult(gate, Verdict.NOT_QUALIFIED, tuple(items), tuple(weak))
    return GateResult(gate, Verdict.PASSED, tuple(items), ())


def overall(results: list[GateResult]) -> str:
    by_gate = {r.gate: r for r in results}
    missing = [g for g in GATES if g not in by_gate]
    if any(r.verdict is Verdict.BLOCKED for r in results):
        return "BLOCKED"
    if missing or any(r.verdict is not Verdict.PASSED for r in results):
        return "NOT_QUALIFIED"
    return "QUALIFIED_PENDING_HUMAN_REVIEW"


def apply_review(outcome: str, reviewer_accepts: bool) -> str:
    """A reviewer can only confirm a passing result or refuse it; a failed outcome stays failed."""
    if outcome != "QUALIFIED_PENDING_HUMAN_REVIEW":
        return outcome
    return "QUALIFIED" if reviewer_accepts else "REFUSED_BY_REVIEWER"


def junit_items(suite: str, counts: dict[str, int] | None, hard: bool) -> list[EvidenceItem]:
    """Turn one test suite's recorded totals into evidence. Missing results are `not_run`, never a pass."""
    if counts is None:
        return [EvidenceItem(f"tests:{suite}", ItemStatus.NOT_RUN, hard, "no junit result recorded")]
    failed = counts.get("failures", 0) + counts.get("errors", 0)
    if failed:
        return [EvidenceItem(f"tests:{suite}", ItemStatus.FAILED, hard, f"{failed} failing")]
    if counts.get("tests", 0) == 0 or counts.get("tests", 0) == counts.get("skipped", 0):
        return [EvidenceItem(f"tests:{suite}", ItemStatus.NOT_RUN, hard, "all tests skipped or none collected")]
    detail = f"{counts['tests'] - counts.get('skipped', 0)} passed, {counts.get('skipped', 0)} skipped"
    status = ItemStatus.PASSED if counts.get("skipped", 0) == 0 else ItemStatus.UNQUALIFIED
    return [EvidenceItem(f"tests:{suite}", status, hard, detail)]
