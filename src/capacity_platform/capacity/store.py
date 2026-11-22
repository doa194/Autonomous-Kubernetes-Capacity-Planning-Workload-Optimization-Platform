"""Capacity persistence (svc_capacity role): forecasts, analyses, candidates, evaluations.

Writes are idempotent by content digest: re-processing the same job yields the same
digest and becomes a no-op. A different result under the same idempotency key is a
conflict that is surfaced, never an overwrite (S14 §12, S13 §7.4).
"""

from __future__ import annotations

from capacity_platform.capacity.forecasting.contracts import ForecastResult
from capacity_platform.capacity.simulation.evaluate import AnalysisResult
from capacity_platform.contracts.errors import ErrorCategory, PlatformError
from capacity_platform.shared.db import Connection, emit_event, jsonb


class CapacityStore:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    def save_forecast(self, result: ForecastResult, idempotency_key: str, correlation_id: str) -> str:
        existing = self.conn.execute(
            "SELECT forecast_id FROM capacity.forecast_run WHERE idempotency_key = %s", (idempotency_key,)
        ).fetchone()
        if existing:
            if existing["forecast_id"] != result.forecastId:
                raise PlatformError(
                    "IDEMPOTENCY_CONFLICT", ErrorCategory.CONFLICT, "forecast key already bound to different inputs"
                )
            return result.forecastId
        req = result.request
        self.conn.execute(
            "INSERT INTO capacity.forecast_run (forecast_id, idempotency_key, input_digest, cluster_incarnation, "
            "namespace, workload_id, resource, horizon, status, policy_revision, model_family, reason_codes, result) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (forecast_id) DO NOTHING",
            (
                result.forecastId,
                idempotency_key,
                result.inputDigest,
                req.clusterIncarnationRef,
                req.namespace,
                req.logicalWorkloadRef,
                req.resource.value,
                req.horizon.value,
                result.status,
                result.eligibility.policyRevision,
                result.selectedModel.family if result.selectedModel else None,
                jsonb(result.eligibility.reasonCodes),
                jsonb(result),
            ),
        )
        event = "ForecastCompleted.v1" if result.status == "available" else "ForecastInsufficient.v1"
        emit_event(
            self.conn,
            "capacity",
            event,
            result.forecastId,
            {"forecastId": result.forecastId, "status": result.status, "reasonCodes": result.eligibility.reasonCodes},
            correlation_id,
        )
        return result.forecastId

    def load_forecast(self, forecast_id: str) -> ForecastResult | None:
        row = self.conn.execute(
            "SELECT result FROM capacity.forecast_run WHERE forecast_id = %s", (forecast_id,)
        ).fetchone()
        return ForecastResult.model_validate(row["result"]) if row else None

    def save_analysis(self, result: AnalysisResult, namespace: str, workload_id: str, correlation_id: str) -> str:
        inserted = self.conn.execute(
            "INSERT INTO capacity.analysis (analysis_digest, target_key, workload_id, namespace, policy_digest, "
            "status, "
            "result) VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (analysis_digest) DO NOTHING RETURNING analysis_digest",
            (
                result.analysisDigest,
                result.targetKey,
                workload_id,
                namespace,
                result.policyDigest,
                result.status,
                jsonb(result),
            ),
        ).fetchone()
        if inserted is None:
            return result.analysisDigest  # identical analysis already finalized
        for ev in result.evaluations:
            cand = ev.candidate
            self.conn.execute(
                "INSERT INTO capacity.candidate (candidate_digest, analysis_digest, namespace, workload_id, content) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (cand.candidateDigest, result.analysisDigest, namespace, workload_id, jsonb(cand)),
            )
            self.conn.execute(
                "INSERT INTO capacity.evaluation (integrity_digest, candidate_digest, analysis_digest, status, rank, "
                "content) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (ev.integrityDigest, cand.candidateDigest, result.analysisDigest, ev.status.value, ev.rank, jsonb(ev)),
            )
        emit_event(
            self.conn,
            "capacity",
            "CandidateEvaluationCompleted.v1",
            result.analysisDigest,
            {
                "analysisDigest": result.analysisDigest,
                "qualified": len(result.qualified()),
                "total": len(result.evaluations),
            },
            correlation_id,
        )
        return result.analysisDigest

    def load_analysis(self, analysis_digest: str) -> AnalysisResult | None:
        row = self.conn.execute(
            "SELECT result FROM capacity.analysis WHERE analysis_digest = %s", (analysis_digest,)
        ).fetchone()
        return AnalysisResult.model_validate(row["result"]) if row else None
