"""Versioned SQL migration runner with a checksum ledger (S13 §7.5, S29).

Migrations run under a dedicated migration identity during controlled maintenance, never
at service start-up. An already-applied file whose checksum changed is treated as drift or
tampering and stops the run instead of being silently re-applied.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "db" / "migrations"

LEDGER_DDL = """
CREATE SCHEMA IF NOT EXISTS platform_meta;
CREATE TABLE IF NOT EXISTS platform_meta.schema_migrations (
    version     text        PRIMARY KEY,
    checksum    text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


class MigrationDrift(RuntimeError):
    pass


def _files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.sql") if p.name[:4].isdigit())


def apply_migrations(dsn: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations in order. Returns the versions applied by this call."""
    applied_now: list[str] = []
    with psycopg.connect(dsn, autocommit=False) as conn:
        conn.execute(LEDGER_DDL)
        conn.commit()
        done = {r[0]: r[1] for r in conn.execute("SELECT version, checksum FROM platform_meta.schema_migrations")}
        for path in _files(directory):
            version = path.stem
            body = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(body.encode()).hexdigest()
            if version in done:
                if done[version] != checksum:
                    raise MigrationDrift(f"{version} changed after it was applied")
                continue
            with conn.transaction():
                conn.execute(body)
                conn.execute(
                    "INSERT INTO platform_meta.schema_migrations (version, checksum) VALUES (%s, %s)",
                    (version, checksum),
                )
            applied_now.append(version)
    return applied_now


def schema_version(dsn: str) -> list[str]:
    with psycopg.connect(dsn) as conn:
        return [r[0] for r in conn.execute("SELECT version FROM platform_meta.schema_migrations ORDER BY version")]
