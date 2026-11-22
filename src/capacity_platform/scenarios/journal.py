"""Host SQLite run journal with leases and fencing (S05 §5.2, §9.1).

The journal is the experiment runner's own execution record, kept on the host outside the
kind cluster. It is NOT the platform's authority for plans or approvals. Every external
action is journaled as an intent before it happens and reconciled by Kubernetes UID after;
cleanup deletes only objects whose UID the journal recorded.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, scenario_id TEXT NOT NULL, scenario_revision INTEGER NOT NULL,
  scenario_digest TEXT NOT NULL, plan_digest TEXT NOT NULL, seed INTEGER NOT NULL,
  execution_class TEXT NOT NULL, state TEXT NOT NULL, outcome TEXT, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, abort_requested INTEGER NOT NULL DEFAULT 0, abort_reason TEXT);
CREATE TABLE IF NOT EXISTS transitions (
  run_id TEXT NOT NULL, at TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intents (
  intent_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, action TEXT NOT NULL, kind TEXT NOT NULL,
  namespace TEXT NOT NULL, name TEXT NOT NULL, uid TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL,
  detail TEXT);
CREATE TABLE IF NOT EXISTS lease (
  scope TEXT PRIMARY KEY, owner_run_id TEXT NOT NULL, fencing_generation INTEGER NOT NULL,
  expires_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watchdog (
  id INTEGER PRIMARY KEY CHECK (id = 1), state TEXT NOT NULL, detail TEXT, heartbeat_at TEXT NOT NULL);
"""

# S05 §5.3 lifecycle
LEGAL = {
    "Created": {"Validating"},
    "Validating": {"Rejected", "Preflight"},
    "Preflight": {"Rejected", "Prepared"},
    "Prepared": {"Warmup", "Measuring", "Aborting"},
    "Warmup": {"Measuring", "Aborting"},
    "Measuring": {"Cooldown", "Collecting", "Aborting"},
    "Cooldown": {"Collecting", "Aborting"},
    "Collecting": {"Cleaning", "Aborting"},
    "Aborting": {"Cleaning"},
    "Cleaning": {"Completed", "CleanupFailed"},
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Journal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def create_run(
        self, scenario_id: str, revision: int, scenario_digest: str, plan_digest: str, seed: int, execution_class: str
    ) -> str:
        run_id = f"run-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        self.db.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,0,NULL)",
            (
                run_id,
                scenario_id,
                revision,
                scenario_digest,
                plan_digest,
                seed,
                execution_class,
                "Created",
                None,
                _now(),
                _now(),
            ),
        )
        return run_id

    def state(self, run_id: str) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

    def move(self, run_id: str, new: str, reason: str) -> None:
        current = self.state(run_id)["state"]
        if new not in LEGAL.get(current, set()):
            raise ValueError(f"illegal run transition {current} -> {new}")
        self.db.execute("BEGIN IMMEDIATE")
        self.db.execute("UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?", (new, _now(), run_id))
        self.db.execute("INSERT INTO transitions VALUES (?,?,?,?,?)", (run_id, _now(), current, new, reason))
        self.db.execute("COMMIT")

    def set_outcome(self, run_id: str, outcome: str) -> None:
        self.db.execute("UPDATE runs SET outcome = ?, updated_at = ? WHERE run_id = ?", (outcome, _now(), run_id))

    def request_abort(self, run_id: str, reason: str) -> None:
        """Idempotent; used by the CLI and by the independent watchdog."""
        self.db.execute(
            "UPDATE runs SET abort_requested = 1, abort_reason = coalesce(abort_reason, ?) WHERE run_id = ?",
            (reason, run_id),
        )

    def abort_requested(self, run_id: str) -> str | None:
        row = self.state(run_id)
        return row["abort_reason"] if row and row["abort_requested"] else None

    # ---- intents and owned resources ---------------------------------------------------
    def intent(self, run_id: str, action: str, kind: str, namespace: str, name: str) -> str:
        intent_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO intents VALUES (?,?,?,?,?,?,NULL,'intended',?,NULL)",
            (intent_id, run_id, action, kind, namespace, name, _now()),
        )
        return intent_id

    def resolve(self, intent_id: str, status: str, uid: str | None = None, detail: dict | None = None) -> None:
        self.db.execute(
            "UPDATE intents SET status = ?, uid = coalesce(?, uid), detail = ? WHERE intent_id = ?",
            (status, uid, json.dumps(detail or {}), intent_id),
        )

    def owned(self, run_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM intents WHERE run_id = ? AND action = 'create' AND status IN "
            "('created', 'intended', 'uncertain')",
            (run_id,),
        ).fetchall()

    def open_runs(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM runs WHERE state NOT IN ('Completed', 'Rejected', 'CleanupFailed')"
        ).fetchall()

    # ---- exclusive lease with fencing ----------------------------------------------------
    def acquire(self, scope: str, run_id: str, ttl: timedelta = timedelta(minutes=2)) -> int | None:
        self.db.execute("BEGIN IMMEDIATE")
        row = self.db.execute("SELECT * FROM lease WHERE scope = ?", (scope,)).fetchone()
        now = datetime.now(UTC)
        if row and row["owner_run_id"] != run_id and datetime.fromisoformat(row["expires_at"]) > now:
            self.db.execute("ROLLBACK")
            return None
        generation = (row["fencing_generation"] + 1) if row else 1
        self.db.execute(
            "INSERT OR REPLACE INTO lease VALUES (?,?,?,?,?)",
            (scope, run_id, generation, (now + ttl).isoformat(), now.isoformat()),
        )
        self.db.execute("COMMIT")
        return generation

    def heartbeat(self, scope: str, run_id: str, generation: int, ttl: timedelta = timedelta(minutes=2)) -> bool:
        cur = self.db.execute(
            "UPDATE lease SET expires_at = ?, heartbeat_at = ? WHERE scope = ? AND owner_run_id = ? "
            "AND fencing_generation = ?",
            ((datetime.now(UTC) + ttl).isoformat(), _now(), scope, run_id, generation),
        )
        return cur.rowcount == 1  # False: lease lost, stop issuing new actions

    def release(self, scope: str, run_id: str) -> None:
        self.db.execute("DELETE FROM lease WHERE scope = ? AND owner_run_id = ?", (scope, run_id))

    # ---- watchdog heartbeat ------------------------------------------------------------
    def watchdog_beat(self, state: str, detail: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO watchdog VALUES (1, ?, ?, ?)", (state, json.dumps(detail), _now()))

    def watchdog_status(self, max_age: timedelta = timedelta(seconds=45)) -> str:
        row = self.db.execute("SELECT * FROM watchdog WHERE id = 1").fetchone()
        if row is None or datetime.now(UTC) - datetime.fromisoformat(row["heartbeat_at"]) > max_age:
            return "dead"  # no fresh watchdog -> heavy admissions fail closed (S05 §14)
        return row["state"]
