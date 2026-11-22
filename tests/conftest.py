"""Shared fixtures. A disposable PostgreSQL + pgvector container (Testcontainers) is started
lazily, only when a test requests `pg`/`role_dsn`, so pure unit tests never need Docker.

The container runs the same pinned image as the cluster. Migrations are applied as the
superuser (the migration identity); each test then connects as the real service role, so
grant and row-level-security behaviour is exercised for real, not mocked.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

import psycopg
import pytest

from capacity_platform.shared.migrate import apply_migrations

PG_IMAGE = "pgvector/pgvector:0.8.1-pg17"
SERVICE_ROLES = ("svc_api", "svc_observer", "svc_capacity", "svc_ai", "svc_governance", "svc_executor", "svc_auditor")


@pytest.fixture(scope="session")
def pg():
    testcontainers = pytest.importorskip("testcontainers.postgres")
    from testcontainers.core.config import testcontainers_config

    # PostgreSQL initialises, then restarts once; on a busy laptop that can exceed the 120 s default.
    testcontainers_config.max_tries = 600  # with 1 s between tries; `timeout` is derived from this
    try:
        container = testcontainers.PostgresContainer(
            PG_IMAGE, username="migrator", password="migrator-test", dbname="capacity"
        )
        container.start()
    except Exception as exc:  # Docker not available: skip honestly, never pass
        pytest.skip(f"Docker/Testcontainers unavailable: {exc}")
    host, port = container.get_container_host_ip(), container.get_exposed_port(5432)
    admin = f"postgresql://migrator:migrator-test@{host}:{port}/capacity"
    apply_migrations(admin)
    passwords = {role: secrets.token_urlsafe(16) for role in SERVICE_ROLES}
    with psycopg.connect(admin, autocommit=True) as conn:
        for role, pw in passwords.items():
            conn.execute(f"ALTER ROLE {role} LOGIN PASSWORD '{pw}'")
    yield {"admin": admin, "host": host, "port": port, "passwords": passwords}
    container.stop()


@pytest.fixture(scope="session")
def role_dsn(pg) -> Callable[[str], str]:
    def make(role: str) -> str:
        return f"postgresql://{role}:{pg['passwords'][role]}@{pg['host']}:{pg['port']}/capacity"

    return make


@pytest.fixture()
def admin_conn(pg):
    with psycopg.connect(pg["admin"], autocommit=True) as conn:
        yield conn
