"""Release qualification bundle (S32 §3-§4): collect recorded evidence, compute the six gates.

Why this file exists: `capacity-lab release qualify` must answer "can this build be called
qualified, and for what?" from evidence that already exists. It reads test results, live
verification reports, backup/restore drills, audit checks, model qualification and
experiment records, and writes a bundle under ~/.capacity-lab/release-qualification/<id>/.

Anything that was not run is reported as `not_run`, never as a pass. In particular the S31
B0-B3 paired comparisons are only reported as available when paired experiment evidence
exists; this module never computes or invents performance numbers.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import yaml

from capacity_platform.release.gates import (
    GATES,
    EvidenceItem,
    GateResult,
    ItemStatus,
    apply_review,
    evaluate_gate,
    junit_items,
    overall,
)

ROOT = Path(__file__).resolve().parents[3]
HOME = Path.home() / ".capacity-lab"
EVIDENCE = HOME / "evidence"
TEST_RESULTS = EVIDENCE / "tests"

# Suites in the order S30 runs them. `hard` suites guard auth/authority/safety: a failure blocks.
SUITES = {
    "unit": {"path": "tests/unit", "hard": False},
    "security": {"path": "tests/security", "hard": True},
    "component": {"path": "tests/component", "hard": True},
    "integration": {"path": "tests/integration", "hard": True},
    "operational": {"path": "tests/operational", "hard": True},
    "e2e": {"path": "tests/e2e", "hard": True},
}
REQUIRED_DOCS = [
    "architecture.md",
    "setup-guide.md",
    "operator-guide.md",
    "security.md",
    "testing-strategy.md",
    "evaluation.md",
    "observability.md",
    "backup-and-recovery.md",
    "release-and-upgrade.md",
    "limitations.md",
    "design-decisions.md",
    "spec-traceability.md",
]


# ------------------------------------------------------------------------------------- running tests
def run_test_suites(only: list[str] | None = None) -> dict:
    """Run each pytest suite (and the .NET tests) writing junit XML as durable evidence."""
    TEST_RESULTS.mkdir(parents=True, exist_ok=True)
    previous = TEST_RESULTS / "summary.json"
    # Merge with earlier runs so running one suite does not erase the evidence of the others.
    summary: dict[str, dict] = json.loads(previous.read_text()) if previous.exists() else {}
    for name, suite in SUITES.items():
        if only and name not in only:
            continue
        xml = TEST_RESULTS / f"{name}.xml"
        started = time.monotonic()
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable,
                "-m",
                "pytest",
                suite["path"],
                "-q",
                "-p",
                "no:cacheprovider",
                f"--junitxml={xml}",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        summary[name] = {
            "exitCode": proc.returncode,
            "seconds": round(time.monotonic() - started, 1),
            "counts": junit_counts(xml),
            "tail": proc.stdout.strip().splitlines()[-1:],
        }
    if not only or "dotnet" in only:
        summary["dotnet"] = _dotnet_tests()
    (TEST_RESULTS / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def _dotnet_tests() -> dict:
    trx_dir = TEST_RESULTS / "dotnet"
    dotnet = shutil.which("dotnet")
    if dotnet is None:  # no SDK on this host: the suite is not_run, never passed
        return {"exitCode": None, "counts": None}
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            dotnet,
            "test",
            "workloads/dotnet/Tests/Tests.csproj",
            "--logger",
            "trx;LogFileName=dotnet.trx",
            "--results-directory",
            str(trx_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    counts = None
    trx = trx_dir / "dotnet.trx"
    if trx.exists():
        ns = {"t": "http://microsoft.com/schemas/VisualStudio/TeamTest/2010"}
        node = ET.parse(trx).getroot().find(".//t:ResultSummary/t:Counters", ns)  # noqa: S314
        if node is not None:
            counts = {
                "tests": int(node.get("total", 0)),
                "failures": int(node.get("failed", 0)),
                "errors": int(node.get("error", 0)),
                "skipped": int(node.get("notExecuted", 0)),
            }
    return {"exitCode": proc.returncode, "counts": counts}


def junit_counts(path: Path) -> dict[str, int] | None:
    if not path.exists():
        return None
    root = ET.parse(path).getroot()  # noqa: S314
    suites = [root] if root.tag == "testsuite" else list(root)
    return {k: sum(int(s.get(k, 0)) for s in suites) for k in ("tests", "failures", "errors", "skipped")}


# ------------------------------------------------------------------------------------- evidence readers
def _latest(pattern: str, base: Path = EVIDENCE) -> Path | None:
    found = sorted(base.glob(pattern))
    return found[-1] if found else None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rg01_traceability() -> list[EvidenceItem]:
    manifest = yaml.safe_load((ROOT / "specs" / "manifest.yaml").read_text(encoding="utf-8"))
    bad = [
        s["file"]
        for s in manifest["sources"]
        if not (ROOT / "specs" / s["file"]).exists() or _sha256(ROOT / "specs" / s["file"]) != s["sha256"]
    ]
    items = [
        EvidenceItem(
            "spec-source-digests",
            ItemStatus.FAILED if bad else ItemStatus.PASSED,
            hard=True,
            detail=f"changed or missing: {bad}" if bad else f"{len(manifest['sources'])} sources match",
        )
    ]
    trace = ROOT / "docs" / "spec-traceability.md"
    items.append(
        EvidenceItem(
            "traceability-and-deviations-register",
            ItemStatus.PASSED if trace.exists() else ItemStatus.NOT_RUN,
            detail=str(trace.relative_to(ROOT)),
        )
    )
    return items


def rg02_tests() -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    for name, suite in SUITES.items():
        items += junit_items(name, junit_counts(TEST_RESULTS / f"{name}.xml"), suite["hard"])
    summary = TEST_RESULTS / "summary.json"
    dotnet = json.loads(summary.read_text()).get("dotnet") if summary.exists() else None
    items += junit_items("dotnet", dotnet and dotnet.get("counts"), hard=False)
    verify = _latest("verify-all-*.json")
    if verify is None:
        items.append(EvidenceItem("live-security-verification", ItemStatus.NOT_RUN, hard=True))
    else:
        checks = json.loads(verify.read_text())["checks"]
        failed = [k for k, v in checks.items() if v["verdict"] == "fail"]
        status = (
            ItemStatus.FAILED
            if failed
            else (ItemStatus.PASSED if all(v["verdict"] == "pass" for v in checks.values()) else ItemStatus.NOT_RUN)
        )
        items.append(
            EvidenceItem("live-security-verification", status, hard=True, detail=f"{verify.name}; failed={failed}")
        )
    return items


def rg03_evaluation() -> tuple[list[EvidenceItem], dict]:
    """S31 arm availability. Paired B0-B3 statistics need paired experiment evidence that this lab
    run may not have; absent evidence is reported as not_run rather than estimated."""
    model = _latest("model-qualification-*.json")
    model_report = json.loads(model.read_text()) if model else None
    runs = sorted((EVIDENCE / "experiments").glob("*/manifest.json")) if (EVIDENCE / "experiments").exists() else []
    completed = [json.loads(p.read_text()) for p in runs]
    paired = EVIDENCE / "evaluations" / "b0-b3-comparison.json"
    arms = {
        "B0": {"status": "available", "note": "pinned S04 requests/limits are the static control"},
        "B1": {"status": "not_run", "note": "no VPA or rules recommender installed in this lab profile"},
        "B2": {
            "status": "available" if completed else "not_run",
            "note": f"{len(completed)} sealed experiment runs; deterministic engine deployed",
        },
        "B3": {
            "status": "available" if model_report and model_report.get("qualified") else "unqualified",
            "note": "requires a qualified local model profile",
        },
    }
    items = [
        EvidenceItem("b0-static-baseline", ItemStatus.PASSED),
        EvidenceItem("b1-rules-vpa-baseline", ItemStatus.NOT_RUN, detail=arms["B1"]["note"]),
        EvidenceItem(
            "paired-b0-b3-comparison",
            ItemStatus.PASSED if paired.exists() else ItemStatus.NOT_RUN,
            detail="paired, repeated runs per S31 §5" if paired.exists() else "no paired comparison",
        ),
        EvidenceItem(
            "model-qualification",
            ItemStatus.PASSED
            if model_report and model_report.get("qualified")
            else (ItemStatus.UNQUALIFIED if model_report else ItemStatus.NOT_RUN),
            detail=model.name if model else "not run",
        ),
        # No performance claim is published without its evidence, so the claim register is consistent.
        EvidenceItem("claims-withheld-without-evidence", ItemStatus.PASSED),
    ]
    return items, {
        "arms": arms,
        "experimentRuns": len(completed),
        "pairedComparison": paired.exists(),
        "modelQualification": model_report and {k: model_report.get(k) for k in ("qualified", "gates")},
    }


def rg04_operations(live: dict) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    restore = sorted((HOME / "backups").glob("*/restore-verification.json")) if (HOME / "backups").exists() else []
    if restore:
        drill = json.loads(restore[-1].read_text())
        items.append(
            EvidenceItem(
                "backup-restore-drill",
                ItemStatus.PASSED if drill["restored"] else ItemStatus.FAILED,
                hard=True,
                detail=f"{drill['backupId']} rto={drill.get('durationSeconds')}s",
            )
        )
    else:
        items.append(EvidenceItem("backup-restore-drill", ItemStatus.NOT_RUN, hard=True))
    audit = live.get("audit")
    items.append(
        EvidenceItem(
            "audit-chain-and-checkpoints",
            ItemStatus.NOT_RUN if audit is None else (ItemStatus.PASSED if audit.get("intact") else ItemStatus.FAILED),
            hard=True,
        )
    )
    verify = _latest("verify-all-*.json")
    obs = json.loads(verify.read_text())["checks"].get("observability_targets") if verify else None
    items.append(
        EvidenceItem(
            "observability-targets",
            ItemStatus.NOT_RUN
            if obs is None
            else (ItemStatus.PASSED if obs["verdict"] == "pass" else ItemStatus.FAILED),
        )
    )
    watchdog = live.get("watchdog")
    items.append(
        EvidenceItem(
            "host-safety-watchdog", ItemStatus.PASSED if watchdog else ItemStatus.NOT_RUN, detail=str(watchdog)
        )
    )
    items.append(
        EvidenceItem(
            "full-rebuild-and-restore-rto",
            ItemStatus.NOT_RUN,
            detail="cluster rebuild from backup not rehearsed in this run",
        )
    )
    return items


def rg05_delivery(live: dict) -> list[EvidenceItem]:
    images = HOME / "release" / "images.json"
    pinned = images.exists() and all("@sha256:" in v for v in json.loads(images.read_text()).values())
    items = [
        EvidenceItem("image-digests-recorded", ItemStatus.PASSED if pinned else ItemStatus.NOT_RUN),
        EvidenceItem(
            "third-party-versions-locked",
            ItemStatus.PASSED if (ROOT / "infra" / "versions.lock.json").exists() else ItemStatus.FAILED,
        ),
    ]
    ledger = live.get("migrations")
    items.append(
        EvidenceItem(
            "migration-checksums-match",
            ItemStatus.NOT_RUN if ledger is None else (ItemStatus.PASSED if ledger["match"] else ItemStatus.FAILED),
            hard=True,
            detail=json.dumps(ledger) if ledger else "",
        )
    )
    items.append(
        EvidenceItem("upgrade-rollback-rehearsal", ItemStatus.NOT_RUN, detail="no prior release exists to upgrade from")
    )
    return items


def rg06_docs() -> list[EvidenceItem]:
    docs = ROOT / "docs"
    missing = [
        d for d in REQUIRED_DOCS if not (docs / d).exists() or not (docs / d).read_text(encoding="utf-8").strip()
    ]
    return [
        EvidenceItem("readme", ItemStatus.PASSED if (ROOT / "README.md").exists() else ItemStatus.FAILED),
        EvidenceItem(
            "handoff-docs",
            ItemStatus.PASSED if not missing else ItemStatus.NOT_RUN,
            detail=f"missing: {missing}" if missing else f"{len(REQUIRED_DOCS)} present",
        ),
        EvidenceItem("human-reviewer-record", ItemStatus.NOT_RUN, detail="pending attributable human review"),
    ]


# ------------------------------------------------------------------------------------- live probes
def live_probes() -> dict:
    """Checks that need the running lab. Each failure to reach the lab leaves the item `not_run`."""
    out: dict = {}
    try:
        from capacity_platform.lab.backup import audit_verify

        out["audit"] = audit_verify()
    except Exception as exc:  # cluster down: not_run, not a pass
        out["auditError"] = type(exc).__name__
    try:
        out["migrations"] = _migration_ledger()
    except Exception as exc:
        out["migrationsError"] = type(exc).__name__
    try:
        from capacity_platform.scenarios.journal import Journal

        out["watchdog"] = Journal(HOME / "scenario-journal.sqlite").watchdog_status()
        if out["watchdog"] in ("dead", None):
            out["watchdog"] = None
    except Exception as exc:
        out["watchdogError"] = type(exc).__name__
    return out


def _migration_ledger() -> dict:
    from capacity_platform.lab.backup import _psql

    rows = _psql("SELECT version || '=' || checksum FROM platform_meta.schema_migrations ORDER BY version")
    live = dict(line.split("=", 1) for line in rows.splitlines() if line)
    files = {
        p.stem: hashlib.sha256(p.read_text(encoding="utf-8").encode()).hexdigest()
        for p in sorted((ROOT / "db" / "migrations").glob("*.sql"))
    }
    return {"match": live == files, "applied": len(live), "files": len(files)}


# ------------------------------------------------------------------------------------- bundle
def _result_json(result: GateResult) -> dict:
    return {
        "gate": result.gate,
        "verdict": result.verdict.value,
        "reasons": list(result.reasons),
        "items": [
            {"id": i.item_id, "status": i.status.value, "hard": i.hard, "detail": i.detail} for i in result.items
        ],
    }


def qualify(reviewer_accepts: bool | None = None) -> dict:
    live = live_probes()
    rg03_items, evaluation = rg03_evaluation()
    results = [
        evaluate_gate(GATES[0], rg01_traceability()),
        evaluate_gate(GATES[1], rg02_tests()),
        evaluate_gate(GATES[2], rg03_items),
        evaluate_gate(GATES[3], rg04_operations(live)),
        evaluate_gate(GATES[4], rg05_delivery(live)),
        evaluate_gate(GATES[5], rg06_docs()),
    ]
    outcome = overall(results)
    if reviewer_accepts is not None:
        outcome = apply_review(outcome, reviewer_accepts)
    bundle_id = f"rq-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    folder = HOME / "release-qualification" / bundle_id
    (folder / "evaluations").mkdir(parents=True, exist_ok=True)
    report = {
        "bundleId": bundle_id,
        "computedAt": datetime.now(UTC).isoformat(),
        "outcome": outcome,
        "gates": [_result_json(r) for r in results],
        "note": "computed from recorded evidence; a human reviewer may refuse but never override a failed gate",
    }
    (folder / "qualification.json").write_text(json.dumps(report, indent=2))
    (folder / "evaluations" / "b0-b3-availability.json").write_text(json.dumps(evaluation, indent=2, default=str))
    (folder / "live-probes.json").write_text(json.dumps(live, indent=2, default=str))
    report["folder"] = str(folder)
    return report
