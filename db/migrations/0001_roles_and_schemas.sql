-- Seven authoritative schemas on one PostgreSQL server (S02-ADR-006, S13).
-- Each domain has a NOLOGIN owner group role; each service logs in with its own role that
-- is a member of exactly the groups it needs. Passwords are never set here: the bootstrap
-- (capacity-lab) sets them from protected inputs, so no credential ever lives in Git.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY[
    'svc_api', 'svc_observer', 'svc_capacity', 'svc_ai', 'svc_governance', 'svc_executor', 'svc_auditor'
  ] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', r);
    END IF;
  END LOOP;
END $$;

CREATE SCHEMA IF NOT EXISTS inventory;
CREATE SCHEMA IF NOT EXISTS capacity;
CREATE SCHEMA IF NOT EXISTS knowledge;
CREATE SCHEMA IF NOT EXISTS governance;
CREATE SCHEMA IF NOT EXISTS execution;
CREATE SCHEMA IF NOT EXISTS audit;
CREATE SCHEMA IF NOT EXISTS workflow;

-- Nobody gets anything by default; grants below and in later migrations are explicit.
-- Extensions live in public; services may use their types/functions but never create objects there.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA inventory, capacity, knowledge, governance, execution, audit, workflow FROM PUBLIC;

GRANT USAGE ON SCHEMA inventory  TO svc_observer, svc_capacity, svc_api, svc_governance, svc_executor;
GRANT USAGE ON SCHEMA capacity   TO svc_capacity, svc_api, svc_governance, svc_ai;
GRANT USAGE ON SCHEMA knowledge  TO svc_ai, svc_api;
GRANT USAGE ON SCHEMA governance TO svc_governance, svc_executor, svc_api;
GRANT USAGE ON SCHEMA execution  TO svc_executor, svc_governance, svc_api;
GRANT USAGE ON SCHEMA audit      TO svc_api, svc_observer, svc_capacity, svc_ai, svc_governance, svc_executor, svc_auditor;
GRANT USAGE ON SCHEMA workflow   TO svc_observer, svc_capacity, svc_ai, svc_governance, svc_executor, svc_api;

-- Helper used by immutability triggers: refuse any UPDATE/DELETE on finalized evidence.
CREATE OR REPLACE FUNCTION workflow.reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'immutable record: % on %.% is not permitted', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
    USING ERRCODE = 'insufficient_privilege';
END $$;
