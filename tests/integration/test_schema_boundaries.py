"""S02-TEST-007 / S13 §7.4: real DB role boundaries, immutability and the audit chain."""

from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

from capacity_platform.shared import jobs
from capacity_platform.shared.db import audit, connect
from capacity_platform.shared.migrate import MigrationDrift, apply_migrations, schema_version

pytestmark = pytest.mark.integration


def test_all_migrations_applied_and_rerun_is_noop(pg):
    versions = schema_version(pg["admin"])
    assert versions[0] == "0001_roles_and_schemas" and len(versions) >= 7
    assert apply_migrations(pg["admin"]) == []


def test_changed_migration_file_is_detected_as_drift(pg, tmp_path: Path):
    from capacity_platform.shared.migrate import MIGRATIONS_DIR

    for src in sorted(MIGRATIONS_DIR.glob("*.sql")):
        (tmp_path / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    first = sorted(tmp_path.glob("*.sql"))[0]
    first.write_text(first.read_text(encoding="utf-8") + "\n-- tampered\n", encoding="utf-8")
    with pytest.raises(MigrationDrift):
        apply_migrations(pg["admin"], tmp_path)


@pytest.mark.parametrize(
    ("role", "statement"),
    [
        ("svc_ai", "INSERT INTO governance.operating_mode (mode, revision, changed_by) VALUES ('Governed', 9, 'ai')"),
        ("svc_ai", "UPDATE governance.operating_mode SET mode = 'Governed'"),
        ("svc_executor", "UPDATE governance.plan_state SET state = 'Approved'"),
        ("svc_governance", "INSERT INTO execution.target_lock VALUES ('k', 'i', 'h', 1, now())"),
        ("svc_capacity", "INSERT INTO inventory.cluster_incarnation VALUES ('x', 'y', 'z', now())"),
        ("svc_api", "INSERT INTO governance.approval_revocation VALUES ('a', 'b', 'c', now())"),
        ("svc_observer", "SELECT * FROM governance.executor_authority_v"),
        ("svc_governance", "SELECT * FROM governance.executor_authority_v"),
    ],
)
def test_cross_schema_writes_and_privileged_views_are_denied(role_dsn, role, statement):
    with connect(role_dsn(role)) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement)


def test_owner_can_write_its_own_schema(role_dsn):
    with connect(role_dsn("svc_governance")) as conn:
        conn.execute(
            "UPDATE governance.operating_mode SET mode = 'Advisory', revision = revision + 1, changed_by = 'test'"
        )
        conn.rollback()


def test_job_queue_row_level_security(role_dsn):
    with connect(role_dsn("svc_governance")) as gov:
        jobs.enqueue(
            gov,
            queue="executor",
            kind="execute_intent",
            payload={"intentId": "i-rls"},
            idempotency_key="rls-1",
            correlation_id="c",
        )
        gov.commit()
    with connect(role_dsn("svc_capacity")) as cap:
        assert jobs.claim(cap, queue="executor", worker="w", lease=timedelta(seconds=30)) is None
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            jobs.enqueue(cap, queue="executor", kind="x", payload={}, idempotency_key="evil", correlation_id="c")
    with connect(role_dsn("svc_executor")) as ex:
        seen = []
        while (job := jobs.claim(ex, queue="executor", worker="w", lease=timedelta(seconds=30))) is not None:
            assert job.queue == "executor"  # RLS: the executor only ever sees its own queue
            seen.append(job.payload.get("intentId"))
            assert jobs.complete(ex, job, {"ok": True})
        ex.commit()
        assert "i-rls" in seen


def test_idempotent_enqueue_and_conflicting_payload(role_dsn):
    with connect(role_dsn("svc_api")) as api:
        a = jobs.enqueue(
            api, queue="capacity", kind="forecast", payload={"w": 1}, idempotency_key="idem-1", correlation_id="c"
        )
        b = jobs.enqueue(
            api, queue="capacity", kind="forecast", payload={"w": 1}, idempotency_key="idem-1", correlation_id="c"
        )
        assert a == b
        with pytest.raises(jobs.IdempotencyConflict):
            jobs.enqueue(
                api, queue="capacity", kind="forecast", payload={"w": 2}, idempotency_key="idem-1", correlation_id="c"
            )
        api.commit()


def test_expired_lease_reclaim_fences_out_the_old_worker(role_dsn, admin_conn):
    with connect(role_dsn("svc_api")) as api:
        jobs.enqueue(
            api, queue="capacity", kind="analysis", payload={"n": 7}, idempotency_key="fence-1", correlation_id="c"
        )
        api.commit()
    with connect(role_dsn("svc_capacity")) as cap:
        old = jobs.claim(cap, queue="capacity", worker="old", lease=timedelta(seconds=30))
        cap.commit()
        admin_conn.execute(
            "UPDATE workflow.jobs SET lease_expires_at = now() - interval '1 second' WHERE job_id = %s", (old.job_id,)
        )
        new = jobs.claim(cap, queue="capacity", worker="new", lease=timedelta(seconds=30))
        assert new.job_id == old.job_id and new.fencing_epoch > old.fencing_epoch
        assert not jobs.complete(cap, old, {"stale": True})
        assert jobs.complete(cap, new, {"fresh": True})
        cap.commit()


def test_audit_is_append_only_and_tamper_is_detected(role_dsn, admin_conn):
    with connect(role_dsn("svc_governance")) as gov:
        for i in range(3):
            audit(
                gov,
                correlation_id=f"c{i}",
                action="plan.approve",
                scope={"namespace": "workloads-batch"},
                decision="allow",
                reason="allowed",
                human_ref="sub-approver",
            )
        gov.commit()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            gov.execute("UPDATE audit.events SET reason_code = 'x'")
    assert admin_conn.execute("SELECT audit.verify_chain()").fetchone()[0] is None
    row = admin_conn.execute("SELECT actor_machine FROM audit.events ORDER BY seq DESC LIMIT 1").fetchone()
    assert row[0] == "svc_governance"
    # Even the database owner's UPDATE is blocked by the trigger; disabling it is the only way.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        admin_conn.execute("UPDATE audit.events SET reason_code = 'forged' WHERE seq = 1")
    admin_conn.execute("ALTER TABLE audit.events DISABLE TRIGGER audit_no_update")
    admin_conn.execute("UPDATE audit.events SET reason_code = 'forged' WHERE seq = (SELECT min(seq) FROM audit.events)")
    admin_conn.execute("ALTER TABLE audit.events ENABLE TRIGGER audit_no_update")
    assert admin_conn.execute("SELECT audit.verify_chain()").fetchone()[0] is not None


def test_finalized_records_are_immutable(role_dsn):
    with connect(role_dsn("svc_capacity")) as cap:
        cap.execute(
            "INSERT INTO capacity.query_definition (query_id, revision, expression_digest, unit, definition) "
            "VALUES ('q.test', 1, %s, 'millicore', '{}')",
            ("sha256:" + "a" * 64,),
        )
        cap.commit()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cap.execute("UPDATE capacity.query_definition SET unit = 'byte' WHERE query_id = 'q.test'")
