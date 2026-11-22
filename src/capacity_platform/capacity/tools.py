"""Backend side of the AI evidence tools (S09 §5.2, S17 §3), hosted by `capacity`.

Every call is authorized here independently of the AI-side broker:
1. the caller must be the `capacity-ai-sa` machine identity;
2. `X-Delegation-Ref` must name an `ai` job that is leased right now (fresh delegation);
3. the human who requested that job must currently hold an `evidence.read` grant for the
   target namespace. Results are bounded summaries of immutable evidence; nothing here
   writes anything, so a compromised AI process gains read access at most.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from capacity_platform.capacity.service import current_grants, current_mode
from capacity_platform.capacity.telemetry.registry import REGISTRY, PrometheusClient, render, window
from capacity_platform.contracts.enums import HumanRole
from capacity_platform.contracts.errors import PlatformError, denied
from capacity_platform.contracts.identity import ActorRef
from capacity_platform.contracts.workloads import CATALOG
from capacity_platform.governance.authz import authorize
from capacity_platform.shared.db import Connection


def delegation_scope(conn: Connection, delegation_ref: str, namespace: str) -> ActorRef:
    job = conn.execute(
        "SELECT payload, state, lease_expires_at FROM workflow.jobs WHERE job_id::text = %s AND queue = 'ai'",
        (delegation_ref,),
    ).fetchone()
    if job is None or job["state"] != "leased" or job["lease_expires_at"] < datetime.now(UTC):
        raise denied("delegation_invalid")
    actor = ActorRef(issuer=job["payload"]["actor"]["issuer"], subject=job["payload"]["actor"]["subject"])
    roles = {HumanRole(r) for r in job["payload"].get("roles", [])}
    decision = authorize(
        actor,
        roles,
        current_grants(conn, actor.key()),
        "evidence.read",
        cluster_id="capacity-lab",
        namespace=namespace,
        mode=current_mode(conn),
    )
    if not decision.allow:
        raise denied()
    return actor


def _latest_snapshot(conn):
    return conn.execute(
        "SELECT snapshot_id, incarnation_id, capture_completed, coverage, freshness, material "
        "FROM inventory.snapshot ORDER BY committed_at DESC LIMIT 1"
    ).fetchone()


def _scoped_evaluation(conn, digest_: str, namespaces_ok) -> dict:
    row = conn.execute(
        "SELECT c.namespace, e.content FROM capacity.evaluation e JOIN capacity.candidate c "
        "USING (candidate_digest) WHERE e.integrity_digest = %s",
        (digest_,),
    ).fetchone()
    if row is None or not namespaces_ok(row["namespace"]):
        raise denied()
    return row["content"]


def run_tool(
    conn: Connection,
    tool_id: str,
    args: dict[str, Any],
    delegation_ref: str,
    prom: PrometheusClient | None = None,
) -> dict:
    workload = args.get("workloadId")
    if workload:
        entry = CATALOG.get(workload)
        if entry is None:
            raise denied()
        delegation_scope(conn, delegation_ref, entry.namespace)

    def ns_ok(namespace: str) -> bool:
        delegation_scope(conn, delegation_ref, namespace)
        return True

    if tool_id == "cluster.get_workload_state.v1":
        snap = _latest_snapshot(conn)
        if snap is None:
            return {"status": "insufficient_evidence", "reason": "no_snapshot"}
        w = next(
            (w for w in snap["material"]["workloads"] if w["namespace"] == entry.namespace and w["name"] == entry.name),
            None,
        )
        pods = [
            {k: p[k] for k in ("name", "nodeName", "schedulingStatus", "effectiveRequest", "restarts", "oomKilled")}
            for p in snap["material"]["pods"]
            if w and p.get("controllerUid") == w["uid"]
        ]
        return {
            "sourceRef": str(snap["snapshot_id"]),
            "coverage": snap["coverage"],
            "freshness": snap["freshness"],
            "capturedAt": snap["capture_completed"].isoformat(),
            "workload": w
            and {"uid": w["uid"], "kind": w["kind"], "replicas": w["replicas"], "containers": w["containers"]},
            "pods": pods,
            "semantics": "requests are scheduler reservations, not observed usage",
        }
    if tool_id == "cluster.get_node_fit_inputs.v1":
        snap = _latest_snapshot(conn)
        if snap is None:
            return {"status": "insufficient_evidence", "reason": "no_snapshot"}
        # Node-level totals only; no foreign Pod identities are disclosed.
        return {
            "sourceRef": str(snap["snapshot_id"]),
            "nodes": [
                {k: n[k] for k in ("name", "ready", "unschedulable", "allocatable", "assigned")}
                | {"pool": n["labels"].get("capacity.lab/pool")}
                for n in snap["material"]["nodes"]
            ],
        }
    if tool_id == "forecast.get_capacity_estimate.v1":
        row = conn.execute(
            "SELECT forecast_id, status, result FROM capacity.forecast_run WHERE workload_id = %s "
            "AND resource = %s ORDER BY produced_at DESC LIMIT 1",
            (workload, args["resource"]),
        ).fetchone()
        if row is None:
            return {"status": "insufficient_evidence", "reason": "no_forecast"}
        res = row["result"]
        return {
            "sourceRef": row["forecast_id"],
            "status": row["status"],
            "unit": res["unit"],
            "reasonCodes": res["eligibility"]["reasonCodes"],
            "predictions": [
                {
                    "start": p["startUtc"],
                    "point": round(p["point"], 3),
                    "lower": p["lower"] and round(p["lower"], 3),
                    "upper": p["upper"] and round(p["upper"], 3),
                }
                for p in res["predictions"]
            ][:24],
            "uncertainty": res.get("uncertainty"),
            "model": (res.get("selectedModel") or {}).get("family"),
        }
    if tool_id == "candidate.list_eligible.v1":
        row = conn.execute(
            "SELECT namespace, result FROM capacity.analysis WHERE analysis_digest = %s", (args["analysisDigest"],)
        ).fetchone()
        if row is None or not ns_ok(row["namespace"]):
            raise denied()
        return {
            "sourceRef": args["analysisDigest"],
            "candidates": [
                {
                    "evaluationDigest": e["integrityDigest"],
                    "status": e["status"],
                    "rank": e["rank"],
                    "fields": e["candidate"]["fields"],
                    "reservationDeltaPerPod": e["reservationDeltaPerPod"],
                }
                for e in row["result"]["evaluations"]
            ][:10],
        }
    if tool_id in ("simulation.get_result.v1", "simulation.evaluate_candidate.v1"):
        ev = _scoped_evaluation(conn, args["evaluationDigest"], ns_ok)
        return {
            "sourceRef": args["evaluationDigest"],
            "status": ev["status"],
            "placement": ev["placement"],
            "objectives": ev.get("objectives"),
            "benefitKinds": ev["benefitKinds"],
            "empiricalValidation": ev["empiricalValidation"],
        }
    if tool_id == "risk.get_deterministic_findings.v1":
        ev = _scoped_evaluation(conn, args["evaluationDigest"], ns_ok)
        return {
            "sourceRef": args["evaluationDigest"],
            "status": ev["status"],
            "hardGates": [{k: g[k] for k in ("ruleId", "status", "reasonCode")} for g in ev["hardGates"]],
        }
    if tool_id == "policy.inspect_constraints.v1":
        row = conn.execute(
            "SELECT policy_id, policy_revision, policy_digest FROM governance.effective_policy WHERE workload_id = %s",
            (workload,),
        ).fetchone()
        return {
            "sourceRef": row["policy_digest"] if row else None,
            "policy": row and dict(row),
            "note": "read-only reference; not an approval",
        }
    if tool_id == "metrics.get_approved_window.v1":
        if prom is None:
            return {"status": "insufficient_evidence", "reason": "metric_unavailable"}
        definition = REGISTRY[(args["queryId"], 1)]
        start, end = window(datetime.now(UTC), args["hours"] * 3600)
        try:
            series = prom.range(
                definition,
                render(
                    definition,
                    namespace=entry.namespace,
                    pod_prefix=entry.name,
                    container=entry.container,
                    workload=entry.name,
                ),
                start,
                end,
                60,
            )
        except PlatformError:
            return {"status": "insufficient_evidence", "reason": "metric_unavailable"}
        vals = sorted(v for v in series.values if v is not None)
        if not vals:
            return {"status": "insufficient_evidence", "reason": "no_samples", "flags": list(series.flags)}
        return {
            "sourceRef": f"{definition.query_id}@{definition.revision}:{start.isoformat()}",
            "unit": definition.unit,
            "statistic": definition.statistic,
            "windowStart": start.isoformat(),
            "windowEnd": end.isoformat(),
            "coverage": round(series.coverage, 4),
            "samples": len(vals),
            "mean": round(sum(vals) / len(vals), 4),
            "p95": round(vals[int(0.95 * (len(vals) - 1))], 4),
            "maxSampled": round(vals[-1], 4),
            "flags": list(series.flags),
            "caveat": "sampled values; missing samples are not zero and maxSampled is not a true peak",
        }
    raise denied("tool_unknown")
