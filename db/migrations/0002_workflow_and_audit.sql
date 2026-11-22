-- Durable jobs with leases/fencing, consumer inboxes and the append-only audit chain.
-- At-least-once delivery: handlers must be idempotent; a lease expiry never grants new
-- external authority (S02 §5.4).

CREATE TABLE workflow.jobs (
    job_id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    queue            text        NOT NULL CHECK (queue IN ('capacity', 'ai', 'executor', 'observer')),
    kind             text        NOT NULL,
    payload          jsonb       NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_digest   text        NOT NULL CHECK (payload_digest ~ '^sha256:[0-9a-f]{64}$'),
    idempotency_key  text        NOT NULL,
    enqueued_by      text        NOT NULL DEFAULT current_user,
    state            text        NOT NULL DEFAULT 'queued'
                     CHECK (state IN ('queued', 'leased', 'completed', 'failed', 'cancelled', 'timed_out')),
    attempt          integer     NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts     integer     NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
    lease_owner      text        NULL,
    lease_expires_at timestamptz NULL,
    fencing_epoch    bigint      NOT NULL DEFAULT 0,
    next_visible_at  timestamptz NOT NULL DEFAULT now(),
    correlation_id   text        NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz NULL,
    result           jsonb       NULL,
    error_code       text        NULL,
    schema_version   text        NOT NULL DEFAULT 'job/v1',
    UNIQUE (queue, idempotency_key)
);
CREATE INDEX jobs_claim_idx ON workflow.jobs (queue, state, next_visible_at);

-- Row-level security: each service only sees and claims its own queue; only governance
-- (and the API for user-requested analyses) may enqueue work for other domains.
ALTER TABLE workflow.jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflow.jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY jobs_capacity_rw ON workflow.jobs TO svc_capacity USING (queue = 'capacity') WITH CHECK (queue = 'capacity');
CREATE POLICY jobs_ai_rw       ON workflow.jobs TO svc_ai       USING (queue = 'ai')       WITH CHECK (queue = 'ai');
CREATE POLICY jobs_exec_rw     ON workflow.jobs TO svc_executor USING (queue = 'executor') WITH CHECK (queue = 'executor');
CREATE POLICY jobs_obs_rw      ON workflow.jobs TO svc_observer USING (queue = 'observer') WITH CHECK (queue = 'observer');
CREATE POLICY jobs_gov_insert  ON workflow.jobs FOR INSERT TO svc_governance WITH CHECK (queue IN ('capacity', 'ai', 'executor'));
CREATE POLICY jobs_gov_read    ON workflow.jobs FOR SELECT TO svc_governance USING (true);
CREATE POLICY jobs_api_insert  ON workflow.jobs FOR INSERT TO svc_api WITH CHECK (queue IN ('capacity', 'ai'));
CREATE POLICY jobs_api_read    ON workflow.jobs FOR SELECT TO svc_api USING (true);

GRANT SELECT, UPDATE ON workflow.jobs TO svc_capacity, svc_ai, svc_executor, svc_observer;
GRANT INSERT ON workflow.jobs TO svc_governance, svc_api, svc_capacity, svc_ai, svc_executor, svc_observer;
GRANT SELECT ON workflow.jobs TO svc_governance, svc_api;

-- Consumer-owned dedupe of outbox events (events are facts, delivered at least once).
CREATE TABLE workflow.inbox (
    consumer     text        NOT NULL,
    event_id     uuid        NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer, event_id)
);
ALTER TABLE workflow.inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflow.inbox FORCE ROW LEVEL SECURITY;
CREATE POLICY inbox_own ON workflow.inbox USING (consumer = current_user) WITH CHECK (consumer = current_user);
GRANT SELECT, INSERT ON workflow.inbox TO svc_observer, svc_capacity, svc_ai, svc_governance, svc_executor;

-- ---------------------------------------------------------------------------------------
-- Audit: append-only, hash-chained. Each row stores the hash of the previous row, so a
-- rewritten or deleted row breaks the chain. Because an attacker with DBA rights could
-- recompute the whole chain, capacity-lab exports Ed25519-signed checkpoints of the chain
-- head to a host location outside the database (S08 §8.1); verification compares both.
-- ---------------------------------------------------------------------------------------
CREATE TABLE audit.events (
    seq             bigserial   PRIMARY KEY,
    event_id        uuid        NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    occurred_at     timestamptz NOT NULL DEFAULT now(),
    correlation_id  text        NOT NULL,
    actor_machine   text        NOT NULL DEFAULT current_user,
    actor_human_ref text        NULL,
    action          text        NOT NULL,
    resource_scope  jsonb       NOT NULL,
    decision        text        NOT NULL CHECK (decision IN ('allow', 'deny', 'fact')),
    reason_code     text        NOT NULL,
    details         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    prev_hash       text        NOT NULL,
    event_hash      text        NOT NULL
);

CREATE OR REPLACE FUNCTION audit.event_hash(
    p_prev text, p_eid uuid, p_at timestamptz, p_corr text, p_machine text, p_human text, p_action text,
    p_scope jsonb, p_decision text, p_reason text, p_details jsonb) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT encode(public.digest(p_prev || '|' || p_eid::text || '|' ||
    to_char(p_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '|' || p_corr || '|' || p_machine || '|' ||
    coalesce(p_human, '') || '|' || p_action || '|' || p_scope::text || '|' || p_decision || '|' || p_reason ||
    '|' || p_details::text, 'sha256'), 'hex')
$$;

CREATE OR REPLACE FUNCTION audit.chain_event() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = audit, pg_temp AS $$
DECLARE last_hash text;
BEGIN
  -- Serialize appends so the chain is a single total order.
  PERFORM pg_advisory_xact_lock(424242);
  SELECT event_hash INTO last_hash FROM audit.events ORDER BY seq DESC LIMIT 1;
  NEW.prev_hash := coalesce(last_hash, 'genesis');
  -- session_user, not current_user: inside SECURITY DEFINER current_user is the owner.
  NEW.actor_machine := session_user;
  NEW.event_hash := audit.event_hash(NEW.prev_hash, NEW.event_id, NEW.occurred_at, NEW.correlation_id,
      NEW.actor_machine, NEW.actor_human_ref, NEW.action, NEW.resource_scope, NEW.decision, NEW.reason_code,
      NEW.details);
  RETURN NEW;
END $$;


CREATE TRIGGER audit_chain BEFORE INSERT ON audit.events FOR EACH ROW EXECUTE FUNCTION audit.chain_event();
CREATE TRIGGER audit_no_update BEFORE UPDATE OR DELETE ON audit.events FOR EACH ROW
  EXECUTE FUNCTION workflow.reject_mutation();
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit.events FOR EACH STATEMENT
  EXECUTE FUNCTION workflow.reject_mutation();

GRANT INSERT (correlation_id, actor_human_ref, action, resource_scope, decision, reason_code, details)
  ON audit.events TO svc_api, svc_observer, svc_capacity, svc_ai, svc_governance, svc_executor;
GRANT USAGE ON SEQUENCE audit.events_seq_seq TO svc_api, svc_observer, svc_capacity, svc_ai, svc_governance, svc_executor;
GRANT SELECT ON audit.events TO svc_auditor, svc_api;

-- Recompute the chain; returns the first broken sequence number, or NULL when intact.
CREATE OR REPLACE FUNCTION audit.verify_chain() RETURNS bigint
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = audit, pg_temp AS $$
DECLARE rec record; expected_prev text := 'genesis';
BEGIN
  FOR rec IN SELECT * FROM audit.events ORDER BY seq LOOP
    IF rec.prev_hash <> expected_prev OR rec.event_hash <> audit.event_hash(rec.prev_hash, rec.event_id,
        rec.occurred_at, rec.correlation_id, rec.actor_machine, rec.actor_human_ref, rec.action,
        rec.resource_scope, rec.decision, rec.reason_code, rec.details) THEN
      RETURN rec.seq;
    END IF;
    expected_prev := rec.event_hash;
  END LOOP;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION audit.chain_head() RETURNS TABLE (seq bigint, event_hash text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = audit, pg_temp AS $$
  SELECT seq, event_hash FROM audit.events ORDER BY seq DESC LIMIT 1
$$;

GRANT EXECUTE ON FUNCTION audit.verify_chain(), audit.chain_head() TO svc_auditor, svc_api;
