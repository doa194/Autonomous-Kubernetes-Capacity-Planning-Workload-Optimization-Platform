"""S12 registered PromQL query library and bounded Prometheus client.

No caller — human, agent or service — may submit PromQL. A query is chosen by ID and
revision; the renderer injects selector values that were resolved and validated by the
server (namespace, Pod name pattern from S11 lineage, container name). Values are checked
against a strict character set before rendering, so no label-injection or regex can be
smuggled in. Range, step, series count and deadline are capped server-side.

Every result keeps missing samples as missing (never zero) and reports coverage against
the expected step grid.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from capacity_platform.contracts.canonical import digest
from capacity_platform.contracts.errors import ErrorCategory, PlatformError

_SAFE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True)
class QueryDefinition:
    query_id: str
    revision: int
    template: str
    unit: str
    statistic: str
    max_range_seconds: int
    min_step_seconds: int
    max_series: int

    @property
    def expression_digest(self) -> str:
        return digest({"template": self.template, "unit": self.unit, "revision": self.revision})


REGISTRY: dict[tuple[str, int], QueryDefinition] = {
    (q.query_id, q.revision): q
    for q in (
        QueryDefinition(
            "container.cpu.rate.millicores",
            1,
            '1000 * sum by (pod) (rate(container_cpu_usage_seconds_total{{namespace="{ns}",pod=~"{pod}-.*",'
            'container="{container}"}}[5m]))',
            "millicore",
            "rate",
            7 * 86400,
            15,
            10,
        ),
        QueryDefinition(
            "container.memory.working_set.bytes",
            1,
            'max by (pod) (container_memory_working_set_bytes{{namespace="{ns}",pod=~"{pod}-.*",'
            'container="{container}"}})',
            "byte",
            "max_sampled",
            7 * 86400,
            15,
            10,
        ),
        QueryDefinition(
            "container.cpu.throttle.ratio",
            1,
            'sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{namespace="{ns}",pod=~"{pod}-.*",'
            'container="{container}"}}[5m])) / sum by (pod) (rate(container_cpu_cfs_periods_total{{namespace="{ns}",'
            'pod=~"{pod}-.*",container="{container}"}}[5m]))',
            "ratio",
            "rate",
            7 * 86400,
            15,
            10,
        ),
        QueryDefinition(
            "app.http.p95.seconds",
            1,
            'histogram_quantile(0.95, sum by (le) (rate(lab_request_duration_seconds_bucket{{namespace="{ns}",'
            'workload="{workload}"}}[5m])))',
            "second",
            "pNN_estimate",
            7 * 86400,
            15,
            1,
        ),
        QueryDefinition(
            "app.http.error.ratio",
            1,
            'sum(rate(lab_requests_total{{namespace="{ns}",workload="{workload}",outcome="error"}}[5m])) / '
            'sum(rate(lab_requests_total{{namespace="{ns}",workload="{workload}"}}[5m]))',
            "ratio",
            "rate",
            7 * 86400,
            15,
            1,
        ),
        QueryDefinition(
            "app.http.request.rate",
            1,
            'sum(rate(lab_requests_total{{namespace="{ns}",workload="{workload}"}}[5m]))',
            "per_second",
            "rate",
            7 * 86400,
            15,
            1,
        ),
        QueryDefinition(
            "app.queue.depth",
            1,
            'max(lab_queue_depth{{namespace="{ns}",workload="{workload}"}})',
            "count",
            "max_sampled",
            7 * 86400,
            15,
            1,
        ),
        QueryDefinition(
            "workload.availability.ratio",
            1,
            'min(kube_deployment_status_replicas_available{{namespace="{ns}",deployment="{workload}"}} / '
            'kube_deployment_spec_replicas{{namespace="{ns}",deployment="{workload}"}})',
            "ratio",
            "min_sampled",
            7 * 86400,
            15,
            1,
        ),
        QueryDefinition(
            "container.oom.restarts",
            1,
            'sum(increase(kube_pod_container_status_restarts_total{{namespace="{ns}",pod=~"{pod}-.*",'
            'container="{container}"}}[1h]))',
            "count",
            "interval_summary",
            7 * 86400,
            60,
            10,
        ),
    )
}


def render(definition: QueryDefinition, *, namespace: str, pod_prefix: str, container: str, workload: str) -> str:
    for value in (namespace, pod_prefix, container, workload):
        if not _SAFE.match(value):
            raise PlatformError("selector_invalid", ErrorCategory.VALIDATION, "selector value rejected")
    return definition.template.format(ns=namespace, pod=pod_prefix, container=container, workload=workload)


@dataclass(frozen=True)
class Series:
    """One query result aligned to the step grid. `values[i] is None` means missing."""

    query_id: str
    revision: int
    start: datetime
    step_seconds: int
    values: tuple[float | None, ...]
    expected_points: int
    flags: tuple[str, ...]

    @property
    def coverage(self) -> float:
        present = sum(v is not None for v in self.values)
        return present / self.expected_points if self.expected_points else 0.0

    def max_gap_seconds(self) -> int:
        longest = current = 0
        for v in self.values:
            current = current + 1 if v is None else 0
            longest = max(longest, current)
        return longest * self.step_seconds


class PrometheusClient:
    def __init__(self, base_url: str, timeout_seconds: float = 10.0, max_response_bytes: int = 2_000_000) -> None:
        self._base, self._timeout, self._max_bytes = base_url.rstrip("/"), timeout_seconds, max_response_bytes

    def range(self, definition: QueryDefinition, expression: str, start: datetime, end: datetime, step: int) -> Series:
        if end <= start or (end - start).total_seconds() > definition.max_range_seconds:
            raise PlatformError("range_invalid", ErrorCategory.VALIDATION, "query range outside registered bounds")
        if step < definition.min_step_seconds:
            raise PlatformError("step_invalid", ErrorCategory.VALIDATION, "step below registered minimum")
        try:
            resp = httpx.get(
                f"{self._base}/api/v1/query_range",
                timeout=self._timeout,
                params={"query": expression, "start": start.timestamp(), "end": end.timestamp(), "step": step},
            )
        except httpx.HTTPError as exc:
            raise PlatformError(
                "metric_unavailable", ErrorCategory.UNAVAILABLE, "Prometheus unavailable", retryable=True
            ) from exc
        if resp.status_code != 200 or len(resp.content) > self._max_bytes:
            raise PlatformError(
                "metric_unavailable", ErrorCategory.UNAVAILABLE, "Prometheus query failed", retryable=True
            )
        result = resp.json().get("data", {}).get("result", [])
        return align(definition, result, start, end, step)


def align(definition: QueryDefinition, result: list[dict], start: datetime, end: datetime, step: int) -> Series:
    """Align Prometheus matrix output onto the [start, end) grid; series are combined by max
    (multiple Pods during a rollout), and NaN/Inf samples are dropped as invalid, not zeroed."""
    flags: list[str] = []
    n = int((end - start).total_seconds() // step)
    grid: list[float | None] = [None] * n
    if len(result) > definition.max_series:
        raise PlatformError("too_many_series", ErrorCategory.VALIDATION, "query exceeded its series budget")
    if not result:
        flags.append("target_missing")
    if len(result) > 1:
        flags.append("multiple_series_combined")
    t0 = start.timestamp()
    for series in result:
        for ts, raw in series.get("values", []):
            idx = int((float(ts) - t0) // step)
            if not 0 <= idx < n:
                continue
            value = float(raw)
            if not math.isfinite(value) or value < 0:
                flags.append("invalid_sample_dropped")
                continue
            grid[idx] = value if grid[idx] is None else max(grid[idx], value)  # type: ignore[type-var]
    if any(v is None for v in grid) and result:
        flags.append("scrape_gap")
    return Series(definition.query_id, definition.revision, start, step, tuple(grid), n, tuple(sorted(set(flags))))


def bucketize(series: Series, bucket_seconds: int, how: str) -> list[tuple[datetime, float | None]]:
    """Aggregate step samples into forecast buckets. A bucket with no valid samples is None."""
    per = bucket_seconds // series.step_seconds
    out = []
    for b in range(len(series.values) // per):
        chunk = [v for v in series.values[b * per : (b + 1) * per] if v is not None]
        start = series.start + timedelta(seconds=b * bucket_seconds)
        if not chunk:
            out.append((start, None))
        else:
            out.append((start, max(chunk) if how == "max" else sum(chunk) / len(chunk)))
    return out


def window(end: datetime, seconds: int) -> tuple[datetime, datetime]:
    end = end.astimezone(UTC).replace(microsecond=0)
    return end - timedelta(seconds=seconds), end
