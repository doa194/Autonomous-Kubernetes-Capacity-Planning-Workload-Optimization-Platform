"""PostgreSQL connection helpers shared by every service.

Each service connects with its own role (svc_observer, svc_governance, ...). The DSN is
read from a mounted secret file (`CAPACITY_DB_DSN_FILE`) rather than an environment
variable when available, so the password does not appear in `env` dumps (S08 §4.1).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from capacity_platform.contracts.canonical import canonical_json, digest

# Every platform connection returns rows as dicts (`row["column"]`); this alias says so to the type checker.
Connection = psycopg.Connection[dict[str, Any]]


def dsn_from_environment() -> str:
    path = os.environ.get("CAPACITY_DB_DSN_FILE")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    dsn = os.environ.get("CAPACITY_DB_DSN")
    if not dsn:
        raise RuntimeError("database DSN is not configured (CAPACITY_DB_DSN_FILE or CAPACITY_DB_DSN)")
    return dsn


def connect(dsn: str) -> Connection:
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)


@contextmanager
def transaction(conn: Connection) -> Iterator[Connection]:
    """One local transaction: a domain write and its outbox row commit together or not at all."""
    with conn.transaction():
        yield conn


def jsonb(value: Any) -> Jsonb:
    """Serialize pydantic models / dicts through the canonical encoder (UTC, no NaN)."""
    return Jsonb(json.loads(canonical_json(value)))


def emit_event(
    conn: Connection, schema: str, event_type: str, aggregate_id: str, payload: Any, correlation_id: str
) -> None:
    if schema not in {"inventory", "capacity", "knowledge", "governance", "execution"}:
        raise ValueError(f"unknown outbox schema {schema}")
    conn.execute(
        f"INSERT INTO {schema}.outbox (event_type, aggregate_id, payload, payload_digest, correlation_id) "
        "VALUES (%s, %s, %s, %s, %s)",
        (event_type, aggregate_id, jsonb(payload), digest(payload), correlation_id),
    )


def audit(
    conn: Connection,
    *,
    correlation_id: str,
    action: str,
    scope: dict,
    decision: str,
    reason: str,
    human_ref: str | None = None,
    details: dict | None = None,
) -> None:
    """Append one audit fact. Callers never pass tokens or secret values in `details`."""
    conn.execute(
        "INSERT INTO audit.events (correlation_id, actor_human_ref, action, resource_scope, decision, reason_code, "
        "details) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (correlation_id, human_ref, action, jsonb(scope), decision, reason, jsonb(details or {})),
    )


def already_processed(conn: Connection, consumer: str, event_id: str) -> bool:
    """Consumer-side dedupe for at-least-once outbox delivery."""
    row = conn.execute(
        "INSERT INTO workflow.inbox (consumer, event_id) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING event_id",
        (consumer, event_id),
    ).fetchone()
    return row is None
