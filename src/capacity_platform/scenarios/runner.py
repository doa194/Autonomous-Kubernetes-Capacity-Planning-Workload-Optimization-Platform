"""Host scenario orchestrator (S05 §2): plan -> preflight -> run -> collect -> cleanup -> evidence.

Authority boundaries:
* it uses its own restricted kubeconfig (experiment namespace Job/ConfigMap/Secret create +
  delete, batch Job create/delete in workloads-batch, read of demo workloads); never the kind
  admin context;
* W05/W06 batch Jobs are built ONLY from the templates installed with the workloads (the W05
  CronJob's jobTemplate and the W06 template ConfigMap); a scenario may pick an allowlisted
  profile and the seed, never the image, service account or resources;
* it never patches a demo Deployment's resources or replicas and cannot reset S25 changes;
* every created object is journaled as an intent first and deleted later only by exact UID.
The resulting evidence bundle is content-addressed (manifest + per-file SHA-256).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from capacity_platform.scenarios.capability import issue
from capacity_platform.scenarios.demand import DemandPlan, build_plan
from capacity_platform.scenarios.journal import Journal
from capacity_platform.scenarios.schema import ExperimentScenario, JobAction

NAMESPACE = "capacity-experiments"
BATCH_NAMESPACE = "workloads-batch"
DRIVER_START_TIMEOUT_S = 300
W05_CRONJOB = "analytics-processor"
W06_TEMPLATE = "w06-job-template"
STATE_HOME = Path(os.environ.get("CAPACITY_LAB_HOME", Path.home() / ".capacity-lab"))
MANAGED_BY = "scenario-runner"
DRIVER_RESOURCES = {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "300m", "memory": "192Mi"}}
TARGET_URLS = {
    "w01-latency-api": "http://latency-api.workloads-critical.svc:8080",
    "w02-burst-api": "http://burst-api.workloads-standard.svc:8080",
    "w03-memory-profile": "http://memory-profile.workloads-standard.svc:8080",
    "w04-cpu-worker": "http://cpu-worker.workloads-batch.svc:8080",
}


class PreflightRefused(Exception):
    pass


# Provisional S05 trial-validity limits: a run whose load did not reach the workload as planned is
# not evidence about the workload and must never be reported as a usable result.
MIN_SUCCESS_RATIO = 0.95
MAX_DROP_FRACTION = 0.05


def classify_run(summary: dict) -> dict:
    """`passed_pending_evaluation` only when the planned load was delivered and answered."""
    counts = summary.get("counts") or {}
    completed = counts.get("completed", 0)
    if summary.get("status") == "missing" or not completed:
        return {"outcome": "invalid", "reasons": ["driver_summary_missing_or_empty"]}
    reasons = []
    success = counts.get("response_success", 0) / completed
    if success < MIN_SUCCESS_RATIO:
        reasons.append(f"response_success_ratio_{success:.2f}_below_{MIN_SUCCESS_RATIO}")
    drop = summary.get("dropFraction") or {}
    if drop.get("denominator") and drop.get("numerator", 0) / drop["denominator"] > MAX_DROP_FRACTION:
        reasons.append("dropped_arrivals_above_limit")
    return {
        "outcome": "invalid" if reasons else "passed_pending_evaluation",
        "reasons": reasons,
        "successRatio": round(success, 4),
    }


def batch_job(run_id: str, template_spec: dict[str, Any], action: JobAction, seed: int, index: int) -> dict[str, Any]:
    """A Job for W05/W06 from the installed template, changing only RUN_PROFILE and RUN_SEED."""
    spec = json.loads(json.dumps(template_spec))  # deep copy: the template itself is never altered
    for container in spec["template"]["spec"]["containers"]:
        for env in container.get("env", []):
            if env["name"] == "RUN_PROFILE":
                env["value"] = action.profile
            elif env["name"] == "RUN_SEED":
                env["value"] = str(seed)
    labels = {"capacity.lab/managed-by": MANAGED_BY, "capacity.lab/workload-id": action.target}
    spec["template"].setdefault("metadata", {}).setdefault("labels", {}).update(labels)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"{action.target[:3]}-{run_id[-10:]}-{index}",
            "namespace": BATCH_NAMESPACE,
            "labels": labels,
            "annotations": {"capacity.lab/run-id": run_id},
        },
        "spec": spec,
    }


def driver_job(
    run_id: str, image: str, plan_cm: str, cap_secret: str, scenario: ExperimentScenario, deadline: int
) -> dict[str, Any]:
    targets = {t.workloadId: TARGET_URLS[t.workloadId] for t in scenario.spec.targets if t.workloadId in TARGET_URLS}
    short = run_id[-12:]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"driver-{short}",
            "namespace": NAMESPACE,
            "labels": {
                "capacity.lab/managed-by": MANAGED_BY,
                "capacity.lab/template": "load-driver-v1",
                "capacity.lab/scenario": scenario.metadata.id[:63],
            },
            "annotations": {"capacity.lab/run-id": run_id},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": deadline,
            "ttlSecondsAfterFinished": 900,
            "template": {
                "metadata": {
                    "labels": {"capacity.lab/managed-by": MANAGED_BY, "capacity.lab/template": "load-driver-v1"}
                },
                "spec": {
                    "serviceAccountName": "capacity-driver-sa",
                    "automountServiceAccountToken": False,
                    "restartPolicy": "Never",
                    "priorityClassName": "lab-standard",
                    "nodeSelector": {"capacity.lab/pool": "experiment"},
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "driver",
                            "image": image,
                            "command": ["python", "-m", "capacity_platform.scenarios.driver"],
                            "env": [
                                {"name": "TARGETS_JSON", "value": json.dumps(targets)},
                                {"name": "MAX_INFLIGHT", "value": str(scenario.spec.safety.maxInflightRequests)},
                                {"name": "REQUEST_TIMEOUT", "value": str(scenario.spec.safety.requestTimeoutSeconds)},
                            ],
                            "resources": DRIVER_RESOURCES,
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [
                                {"name": "plan", "mountPath": "/plan", "readOnly": True},
                                {"name": "capability", "mountPath": "/capability", "readOnly": True},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "plan", "configMap": {"name": plan_cm}},
                        {"name": "capability", "secret": {"secretName": cap_secret}},
                    ],
                },
            },
        },
    }


class ScenarioRunner:
    def __init__(self, journal: Journal, evidence_root: Path, signing_key: Path, driver_image: str, kube=None) -> None:
        self.journal, self.evidence_root, self.signing_key, self.driver_image = (
            journal,
            evidence_root,
            signing_key,
            driver_image,
        )
        if kube is None:
            from kubernetes import client, config

            config.load_kube_config(
                config_file=str(STATE_HOME / "kubeconfig"), context="capacity-lab-runner"
            )  # never the admin context
            kube = (client.CoreV1Api(), client.BatchV1Api(), client.AppsV1Api())
        self.core, self.batch, self.apps = kube

    # ------------------------------------------------------------------ preflight
    def preflight(self, scenario: ExperimentScenario, run_id: str | None = None) -> dict:
        report: dict[str, Any] = {"checkedAt": datetime.now(UTC).isoformat(), "checks": {}}
        wd = self.journal.watchdog_status()
        report["checks"]["watchdog"] = wd
        if wd in ("dead", "pressure", "abort"):
            raise PreflightRefused(f"watchdog state {wd}")
        # The run being checked is itself open (state Preflight); only OTHER open runs conflict.
        others = [r for r in self.journal.open_runs() if r["run_id"] != run_id]
        if scenario.spec.execution.executionClass != "light" and others:
            raise PreflightRefused("another run is still open (serial execution required)")
        for target in scenario.spec.targets:
            entry_name = TARGET_URLS.get(target.workloadId, "").split("//")[-1].split(".")[0]
            if entry_name:
                dep = self.apps.read_namespaced_deployment(entry_name, target.namespace)
                report["checks"][target.workloadId] = {
                    "uid": dep.metadata.uid,
                    "resourceVersion": dep.metadata.resource_version,
                    "readyReplicas": dep.status.ready_replicas,
                }
                if not dep.status.ready_replicas:
                    raise PreflightRefused(f"{target.workloadId} not ready")
        quota = self.core.list_namespaced_resource_quota(NAMESPACE).items
        report["checks"]["experimentQuota"] = [q.status.hard for q in quota]
        return report

    # ------------------------------------------------------------------ create with journal
    def _create(self, run_id: str, kind: str, name: str, create_fn, namespace: str = NAMESPACE) -> str | None:
        intent = self.journal.intent(run_id, "create", kind, namespace, name)
        try:
            obj = create_fn()
        except Exception as exc:
            # Response may be lost after the object was created: mark uncertain, reconcile by name+label later.
            self.journal.resolve(intent, "uncertain", detail={"error": type(exc).__name__})
            raise
        self.journal.resolve(intent, "created", uid=obj.metadata.uid)
        return obj.metadata.uid

    def run(self, scenario: ExperimentScenario, seed_override: int | None = None) -> str:
        plan: DemandPlan = build_plan(scenario, seed_override)
        run_id = self.journal.create_run(
            scenario.metadata.id,
            scenario.metadata.revision,
            scenario.digest(),
            plan.planDigest,
            plan.seed,
            scenario.spec.execution.executionClass,
        )
        bundle = self.evidence_root / run_id
        bundle.mkdir(parents=True, exist_ok=True)
        self.journal.move(run_id, "Validating", "schema_valid")
        (bundle / "scenario.resolved.json").write_text(scenario.model_dump_json(indent=2))
        (bundle / "demand-plan.json").write_text(plan.model_dump_json())
        self.journal.move(run_id, "Preflight", "plan_built")
        try:
            report = self.preflight(scenario, run_id)
        except PreflightRefused as exc:
            self.journal.move(run_id, "Rejected", str(exc))
            self.journal.set_outcome(run_id, "aborted")
            return run_id
        (bundle / "preflight.json").write_text(json.dumps(report, indent=2, default=str))
        scope = "exclusive" if scenario.spec.execution.executionClass == "exclusive" else "serial"
        generation = self.journal.acquire(scope, run_id)
        if generation is None:
            self.journal.move(run_id, "Rejected", "lease_unavailable")
            return run_id
        self.journal.move(run_id, "Prepared", "admitted")
        total = sum(p.durationSeconds for p in scenario.spec.phases)
        deadline = min(scenario.spec.execution.maxDurationSeconds, total + 120)
        token, cap_id = issue(
            self.signing_key, run_id, [t.workloadId for t in scenario.spec.targets], ttl_seconds=deadline + 60
        )
        from kubernetes import client

        short = run_id[-12:]
        labels = {"capacity.lab/managed-by": MANAGED_BY}
        cm_name, secret_name = f"plan-{short}", f"cap-{short}"
        self._create(
            run_id,
            "ConfigMap",
            cm_name,
            lambda: self.core.create_namespaced_config_map(
                NAMESPACE,
                client.V1ConfigMap(
                    metadata=client.V1ObjectMeta(name=cm_name, labels=labels),
                    data={"plan.json": plan.model_dump_json()},
                ),
            ),
        )
        self._create(
            run_id,
            "Secret",
            secret_name,
            lambda: self.core.create_namespaced_secret(
                NAMESPACE,
                client.V1Secret(
                    metadata=client.V1ObjectMeta(name=secret_name, labels=labels), string_data={"token": token}
                ),
            ),
        )
        job = driver_job(run_id, self.driver_image, cm_name, secret_name, scenario, deadline)
        self._create(run_id, "Job", job["metadata"]["name"], lambda: self.batch.create_namespaced_job(NAMESPACE, job))
        # The phase clock starts only once the driver is actually running; otherwise the first
        # (possibly measured) phase would contain image-pull time instead of load.
        aborted = (
            None
            if self._wait_driver_running(job["metadata"]["name"], run_id, scope, generation)
            else "driver_not_started"
        )
        if aborted:
            self.journal.move(run_id, "Aborting", aborted)
        started = datetime.now(UTC)
        timeline = []
        seen_measured = False
        batch_jobs: list[str] = []
        for phase in scenario.spec.phases if not aborted else []:
            # Schema guarantees warmup* -> measured+ -> cooldown*, so these moves are always legal.
            if phase.measurement == "included":
                next_state = "Measuring"
                seen_measured = True
            else:
                next_state = "Cooldown" if seen_measured else "Warmup"
            if self.journal.state(run_id)["state"] != next_state:
                self.journal.move(run_id, next_state, f"phase:{phase.id}")
            t0 = datetime.now(UTC)
            for action in phase.actions:
                if isinstance(action, JobAction):
                    batch_jobs.append(self._start_batch(run_id, action, plan.seed, len(batch_jobs)))
            end = time.monotonic() + phase.durationSeconds
            while time.monotonic() < end:
                aborted = self.journal.abort_requested(run_id)
                if aborted or not self.journal.heartbeat(scope, run_id, generation):
                    aborted = aborted or "lease_lost"
                    break
                time.sleep(2)
            timeline.append(
                {
                    "phase": phase.id,
                    "startUtc": t0.isoformat(),
                    "endUtc": datetime.now(UTC).isoformat(),
                    "measurement": phase.measurement,
                }
            )
            if aborted:
                self.journal.move(run_id, "Aborting", aborted)
                break
        (bundle / "phase-results.json").write_text(
            json.dumps({"startedAt": started.isoformat(), "phases": timeline}, indent=2)
        )
        quality = {"outcome": "aborted", "reasons": [aborted]} if aborted else None
        if not aborted:
            self.journal.move(run_id, "Collecting", "phases_complete")
            summary = self._driver_summary(job)
            (bundle / "driver-summary.json").write_text(json.dumps(summary, indent=2))
            quality = classify_run(summary)
            (bundle / "run-quality.json").write_text(json.dumps(quality, indent=2))
            if batch_jobs:
                (bundle / "batch-results.json").write_text(json.dumps(self._batch_results(batch_jobs), indent=2))
        self.journal.move(run_id, "Cleaning", "cleanup_started")
        report = self.cleanup(run_id)
        (bundle / "cleanup-report.json").write_text(json.dumps(report, indent=2))
        self.journal.release(scope, run_id)
        self.journal.move(run_id, "Completed" if report["remaining"] == 0 else "CleanupFailed", "cleanup_verified")
        self.journal.set_outcome(run_id, quality["outcome"] if quality else "aborted")
        self._seal(bundle, run_id, plan, cap_id)
        return run_id

    def _start_batch(self, run_id: str, action: JobAction, seed: int, index: int) -> str:
        if action.target == "w05-analytics-processor":
            template = self.batch.read_namespaced_cron_job(W05_CRONJOB, BATCH_NAMESPACE)
            spec = self.batch.api_client.sanitize_for_serialization(template.spec.job_template.spec)
        else:
            spec = json.loads(self.core.read_namespaced_config_map(W06_TEMPLATE, BATCH_NAMESPACE).data["job.json"])
        job = batch_job(run_id, spec, action, seed, index)
        self._create(
            run_id,
            "Job",
            job["metadata"]["name"],
            lambda: self.batch.create_namespaced_job(BATCH_NAMESPACE, job),
            namespace=BATCH_NAMESPACE,
        )
        return job["metadata"]["name"]

    def _batch_results(self, names: list[str]) -> list[dict]:
        """Job outcome plus why a Pod did not schedule (W06 is EXPECTED to wait when capacity is short)."""
        results = []
        for name in names:
            status = self.batch.read_namespaced_job(name, BATCH_NAMESPACE).status
            pods = self.core.list_namespaced_pod(BATCH_NAMESPACE, label_selector=f"job-name={name}").items
            scheduling = [
                {
                    "pod": p.metadata.name,
                    "phase": p.status.phase,
                    "unschedulableReason": next(
                        (
                            c.message
                            for c in (p.status.conditions or [])
                            if c.type == "PodScheduled" and c.status == "False"
                        ),
                        None,
                    ),
                }
                for p in pods
            ]
            results.append(
                {
                    "job": name,
                    "succeeded": status.succeeded or 0,
                    "failed": status.failed or 0,
                    "active": status.active or 0,
                    "pods": scheduling,
                }
            )
        return results

    def _wait_driver_running(
        self, job_name: str, run_id: str, scope: str, generation: int, timeout_s: int = DRIVER_START_TIMEOUT_S
    ) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.journal.abort_requested(run_id) or not self.journal.heartbeat(scope, run_id, generation):
                return False
            pods = self.core.list_namespaced_pod(NAMESPACE, label_selector=f"job-name={job_name}").items
            if any(p.status.phase == "Running" for p in pods):
                return True
            if any(p.status.phase in ("Failed", "Succeeded") for p in pods):
                return False
            time.sleep(2)
        return False

    def _driver_summary(self, job: dict) -> dict:
        pods = self.core.list_namespaced_pod(NAMESPACE, label_selector=f"job-name={job['metadata']['name']}").items
        for _ in range(60):
            done = self.batch.read_namespaced_job(job["metadata"]["name"], NAMESPACE).status
            if done.succeeded or done.failed:
                break
            time.sleep(5)
        for pod in pods:
            log = self.core.read_namespaced_pod_log(pod.metadata.name, NAMESPACE, tail_lines=50)
            for line in log.splitlines():
                if line.startswith("SUMMARY "):
                    return json.loads(line[8:])
        return {"status": "missing", "reason": "driver summary not captured before TTL"}  # quality failure

    # ------------------------------------------------------------------ cleanup by exact UID
    def cleanup(self, run_id: str) -> dict:
        from kubernetes.client import V1DeleteOptions, V1Preconditions

        deleted, remaining = [], 0
        for row in self.journal.owned(run_id):
            delete = {
                "Job": self.batch.delete_namespaced_job,
                "ConfigMap": self.core.delete_namespaced_config_map,
                "Secret": self.core.delete_namespaced_secret,
            }[row["kind"]]
            if not row["uid"]:
                remaining += 1  # uncertain creation: reconcile manually, never delete by name alone
                continue
            try:
                delete(
                    row["name"],
                    row["namespace"],
                    body=V1DeleteOptions(
                        preconditions=V1Preconditions(uid=row["uid"]), propagation_policy="Background"
                    ),
                )
                self.journal.resolve(row["intent_id"], "deleted")
                deleted.append(row["name"])
            except Exception as exc:
                if getattr(exc, "status", None) == 404:
                    self.journal.resolve(row["intent_id"], "deleted")
                    continue
                remaining += 1
        return {"deleted": deleted, "remaining": remaining, "checkedAt": datetime.now(UTC).isoformat()}

    def _seal(self, bundle: Path, run_id: str, plan: DemandPlan, cap_id: str) -> None:
        checksums = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(bundle.iterdir())
            if p.is_file() and p.name != "manifest.json"
        }
        row = self.journal.state(run_id)
        manifest = {
            "schemaVersion": "experiment-run-manifest/v1alpha1",
            "runId": run_id,
            "scenarioId": row["scenario_id"],
            "scenarioRevision": row["scenario_revision"],
            "scenarioDigest": row["scenario_digest"],
            "planDigest": plan.planDigest,
            "seed": plan.seed,
            "generatorRevision": plan.generatorRevision,
            "capabilityId": cap_id,
            "workloadOrigin": "scenario_generated",
            "measurementOrigin": "observed_live",
            "state": row["state"],
            "outcome": row["outcome"],
            "files": checksums,
            "note": "timestamps and measurements vary between runs; the planned schedule does not",
        }
        data = json.dumps(manifest, indent=2, sort_keys=True).encode()
        (bundle / "manifest.json").write_bytes(data)
        (bundle / "manifest.sha256").write_text(hashlib.sha256(data).hexdigest())


def recover_interrupted(journal: Journal, runner: ScenarioRunner) -> list[str]:
    """On restart: never resume a half-run phase. Mark interrupted runs and clean owned objects."""
    handled = []
    for row in journal.open_runs():
        if row["state"] in ("Created", "Validating", "Preflight"):
            journal.move(row["run_id"], "Rejected" if row["state"] != "Created" else "Validating", "interrupted")
        elif row["state"] not in ("Cleaning",):
            journal.request_abort(row["run_id"], "runner_restarted")
            journal.move(row["run_id"], "Aborting", "runner_restarted")
            journal.move(row["run_id"], "Cleaning", "recovery_cleanup")
        if journal.state(row["run_id"])["state"] == "Cleaning":
            report = runner.cleanup(row["run_id"])
            journal.move(row["run_id"], "Completed" if report["remaining"] == 0 else "CleanupFailed", "recovered")
            journal.set_outcome(row["run_id"], "inconclusive")
        handled.append(row["run_id"])
    return handled
