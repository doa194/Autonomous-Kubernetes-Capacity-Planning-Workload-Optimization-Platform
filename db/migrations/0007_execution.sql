-- execution: S25 operation journal, per-controller target lock with fencing, S26 outcomes.
-- Written only by the executor. A worker may commit only while it holds the current
-- fencing epoch of the target lock; a lease expiry hands the lock to a reconciler, it never
-- grants authority to write to Kubernetes again.

CREATE TABLE execution.target_lock (
    target_key        text        PRIMARY KEY,
    intent_id         text        NOT NULL,
    holder            text        NOT NULL,
    fencing_epoch     bigint      NOT NULL CHECK (fencing_epoch > 0),
    lease_expires_at  timestamptz NOT NULL
);

CREATE TABLE execution.operation (
    intent_id      text        PRIMARY KEY,
    operation_id   text        NOT NULL UNIQUE,
    plan_id        text        NOT NULL,
    plan_revision  integer     NOT NULL,
    target_key     text        NOT NULL,
    state          text        NOT NULL CHECK (state IN (
                     'requested', 'awaiting_revalidation', 'refused', 'in_progress', 'unknown',
                     'applied_pending_verification', 'verification_in_progress', 'verified_success',
                     'verification_failed', 'verification_inconclusive', 'recovery_in_progress',
                     'recovered', 'escalated')),
    attempt        integer     NOT NULL DEFAULT 0,
    fencing_epoch  bigint      NOT NULL,
    content        jsonb       NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX operation_open_idx ON execution.operation (state) WHERE state IN ('in_progress', 'unknown',
  'applied_pending_verification', 'verification_in_progress', 'verification_failed', 'verification_inconclusive',
  'recovery_in_progress');

CREATE TABLE execution.operation_transition (
    seq         bigserial   PRIMARY KEY,
    intent_id   text        NOT NULL,
    from_state  text        NOT NULL,
    to_state    text        NOT NULL,
    reason      text        NOT NULL,
    fence       bigint      NOT NULL,
    at          timestamptz NOT NULL
);
CREATE TRIGGER op_transition_append_only BEFORE UPDATE OR DELETE ON execution.operation_transition
  FOR EACH ROW EXECUTE FUNCTION workflow.reject_mutation();

CREATE TABLE execution.outcome (
    outcome_id     text        PRIMARY KEY,
    intent_id      text        NOT NULL REFERENCES execution.operation (intent_id),
    disposition    text        NOT NULL,
    content        jsonb       NOT NULL,
    outcome_digest text        NOT NULL UNIQUE,
    finished_at    timestamptz NOT NULL
);
CREATE TRIGGER outcome_immutable BEFORE UPDATE OR DELETE ON execution.outcome
  FOR EACH ROW EXECUTE FUNCTION workflow.reject_mutation();

CREATE TABLE execution.outbox (
    event_id        uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type      text        NOT NULL,
    aggregate_id    text        NOT NULL,
    payload         jsonb       NOT NULL,
    payload_digest  text        NOT NULL,
    correlation_id  text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA execution TO svc_executor;
GRANT UPDATE ON execution.operation, execution.target_lock TO svc_executor;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA execution TO svc_executor;
GRANT SELECT ON execution.operation, execution.outcome, execution.outbox TO svc_governance, svc_api;
