"""PostgreSQL-backed durable jobs with leases and fencing (S02 §5.4, S02-ADR-005).

`claim` uses `FOR UPDATE SKIP LOCKED` so two workers never lease the same job. Every
claim increments `fencing_epoch`; `complete`/`fail` only succeed if the caller still holds
that epoch, so a worker whose lease expired cannot overwrite a newer worker's result.
Row-level security limits each service role to its own queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from capacity_platform.contracts.canonical import digest
from capacity_platform.shared.db import Connection, jsonb


@dataclass(frozen=True)
class Job:
    job_id: str
    queue: str
    kind: str
    payload: dict[str, Any]
    attempt: int
    max_attempts: int
    fencing_epoch: int
    correlation_id: str


class IdempotencyConflict(Exception):
    """Same idempotency key reused with a different payload."""


def enqueue(
    conn: Connection,
    *,
    queue: str,
    kind: str,
    payload: dict,
    idempotency_key: str,
    correlation_id: str,
    max_attempts: int = 3,
) -> str:
    payload_digest = digest(payload)
    row = conn.execute(
        "INSERT INTO workflow.jobs (queue, kind, payload, payload_digest, idempotency_key, correlation_id, "
        "max_attempts) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (queue, idempotency_key) DO NOTHING RETURNING job_id",
        (queue, kind, jsonb(payload), payload_digest, idempotency_key, correlation_id, max_attempts),
    ).fetchone()
    if row:
        return str(row["job_id"])
    existing = conn.execute(
        "SELECT job_id, payload_digest FROM workflow.jobs WHERE queue = %s AND idempotency_key = %s",
        (queue, idempotency_key),
    ).fetchone()
    if existing is None or existing["payload_digest"] != payload_digest:
        raise IdempotencyConflict(idempotency_key)
    return str(existing["job_id"])


def claim(conn: Connection, *, queue: str, worker: str, lease: timedelta) -> Job | None:
    with conn.transaction():
        # Poison jobs: a worker died holding them max_attempts times. Park them visibly.
        conn.execute(
            "UPDATE workflow.jobs SET state = 'timed_out', finished_at = now(), error_code = 'attempts_exhausted' "
            "WHERE queue = %s AND state = 'leased' AND lease_expires_at < now() AND attempt >= max_attempts",
            (queue,),
        )
        row = conn.execute(
            """
            SELECT job_id FROM workflow.jobs
            WHERE queue = %s AND next_visible_at <= now() AND attempt < max_attempts
              AND (state = 'queued' OR (state = 'leased' AND lease_expires_at < now()))
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED LIMIT 1
            """,
            (queue,),
        ).fetchone()
        if row is None:
            return None
        claimed = conn.execute(
            """
            UPDATE workflow.jobs
               SET state = 'leased', lease_owner = %s, lease_expires_at = now() + %s,
                   attempt = attempt + 1, fencing_epoch = fencing_epoch + 1
             WHERE job_id = %s
            RETURNING job_id, queue, kind, payload, attempt, max_attempts, fencing_epoch, correlation_id
            """,
            (worker, lease, row["job_id"]),
        ).fetchone()
    assert claimed is not None
    return Job(
        str(claimed["job_id"]),
        claimed["queue"],
        claimed["kind"],
        claimed["payload"],
        claimed["attempt"],
        claimed["max_attempts"],
        claimed["fencing_epoch"],
        claimed["correlation_id"],
    )


def complete(conn: Connection, job: Job, result: dict) -> bool:
    row = conn.execute(
        "UPDATE workflow.jobs SET state = 'completed', finished_at = now(), result = %s "
        "WHERE job_id = %s AND fencing_epoch = %s AND state = 'leased' RETURNING job_id",
        (jsonb(result), job.job_id, job.fencing_epoch),
    ).fetchone()
    return row is not None


def fail(conn: Connection, job: Job, error_code: str, *, retryable: bool) -> bool:
    """Bounded retry with exponential backoff; poison jobs end in `failed` with a reason."""
    terminal = not retryable or job.attempt >= job.max_attempts
    row = conn.execute(
        """
        UPDATE workflow.jobs
           SET state = CASE WHEN %s THEN 'failed' ELSE 'queued' END,
               error_code = %s,
               finished_at = CASE WHEN %s THEN now() ELSE NULL END,
               next_visible_at = now() + make_interval(secs => least(300, power(2, attempt)::int)),
               lease_owner = NULL, lease_expires_at = NULL
         WHERE job_id = %s AND fencing_epoch = %s AND state = 'leased'
        RETURNING job_id
        """,
        (terminal, error_code, terminal, job.job_id, job.fencing_epoch),
    ).fetchone()
    return row is not None
