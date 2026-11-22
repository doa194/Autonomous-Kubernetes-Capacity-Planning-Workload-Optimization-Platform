-- governance: S24 plans, approvals, execution intents, grants and operating mode.
-- Written only by the governance service. Plan content and approvals are immutable;
-- lifecycle state is a separate row updated with compare-and-swap on `version`.

CREATE TABLE governance.operating_mode (
    singleton   boolean     PRIMARY KEY DEFAULT true CHECK (singleton),
    mode        text        NOT NULL CHECK (mode IN ('Observe', 'Advisory', 'Governed')),
    revision    integer     NOT NULL CHECK (revision > 0),
    changed_by  text        NOT NULL,
    changed_at  timestamptz NOT NULL DEFAULT now()
);
INSERT INTO governance.operating_mode (mode, revision, changed_by) VALUES ('Observe', 1, 'migration');

CREATE TABLE governance.role_grant (
    grant_id     text        PRIMARY KEY,
    subject_key  text        NOT NULL,
    role         text        NOT NULL CHECK (role IN ('Viewer', 'Analyst', 'Planner', 'Approver', 'Executor', 'Administrator')),
    cluster_id   text        NOT NULL,
    namespaces   text[]      NOT NULL CHECK (cardinality(namespaces) > 0),
    revision     integer     NOT NULL CHECK (revision > 0),
    granted_by   text        NOT NULL,
    granted_at   timestamptz NOT NULL DEFAULT now(),
    revoked_at   timestamptz NULL,
    revoked_by   text        NULL
);
CREATE INDEX grant_subject_idx ON governance.role_grant (subject_key) WHERE revoked_at IS NULL;

-- The governance-authoritative current effective optimization policy per workload.
CREATE TABLE governance.effective_policy (
    workload_id     text        PRIMARY KEY,
    policy_id       text        NOT NULL,
    policy_revision integer     NOT NULL,
    policy_digest   text        NOT NULL CHECK (policy_digest ~ '^sha256:[0-9a-f]{64}$'),
    deterministic_only_execution_allowed boolean NOT NULL DEFAULT false,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE governance.plan_revision (
    plan_id      text        NOT NULL,
    revision     integer     NOT NULL CHECK (revision > 0),
    plan_digest  text        NOT NULL UNIQUE CHECK (plan_digest ~ '^sha256:[0-9a-f]{64}$'),
    author_key   text        NOT NULL,
    namespace    text        NOT NULL,
    workload_id  text        NOT NULL,
    target_key   text        NOT NULL,
    content      jsonb       NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plan_id, revision)
);
CREATE TRIGGER plan_revision_immutable BEFORE UPDATE OR DELETE ON governance.plan_revision
  FOR EACH ROW EXECUTE FUNCTION workflow.reject_mutation();

CREATE TABLE governance.plan_state (
    plan_id    text        NOT NULL,
    revision   integer     NOT NULL,
    state      text        NOT NULL,
    version    integer     NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plan_id, revision),
    FOREIGN KEY (plan_id, revision) REFERENCES governance.plan_revision (plan_id, revision)
);

CREATE TABLE governance.plan_transition (
    seq        bigserial   PRIMARY KEY,
    plan_id    text        NOT NULL,
    revision   integer     NOT NULL,
    from_state text        NULL,
    to_state   text        NOT NULL,
    reason     text        NOT NULL,
    actor_key  text        NOT NULL,
    at         timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER plan_transition_append_only BEFORE UPDATE OR DELETE ON governance.plan_transition
  FOR EACH ROW EXECUTE FUNCTION workflow.reject_mutation();

CREATE TABLE governance.approval (
    approval_id     text        PRIMARY KEY,
    plan_id         text        NOT NULL,
    revision        integer     NOT NULL,
    plan_digest     text        NOT NULL,
    decision        text        NOT NULL CHECK (decision IN ('approved', 'rejected')),
    approver_key    text        NOT NULL,
    author_key      text        NOT NULL,
    content         jsonb       NOT NULL,
    approval_digest text        NOT NULL,
    issued_at       timestamptz NOT NULL,
    expires_at      timestamptz NOT NULL,
    -- Database-level backstop for separation of duties.
    CHECK (approver_key <> author_key),
    FOREIGN KEY (plan_id, revision) REFERENCES governance.plan_revision (plan_id, revision)
);
CREATE UNIQUE INDEX one_approval_per_revision ON governance.approval (plan_id, revision) WHERE decision = 'approved';
CREATE TRIGGER approval_immutable BEFORE UPDATE OR DELETE ON governance.approval
  FOR EACH ROW EXECUTE FUNCTION workflow.reject_mutation();

CREATE TABLE governance.approval_revocation (
    approval_id text        PRIMARY KEY REFERENCES governance.approval (approval_id),
    revoked_by  text        NOT NULL,
    reason      text        NOT NULL,
    revoked_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE governance.execution_intent (
    intent_id       text        PRIMARY KEY,
    plan_id         text        NOT NULL,
    revision        integer     NOT NULL,
    approval_id     text        NOT NULL REFERENCES governance.approval (approval_id),
    requester_key   text        NOT NULL,
    idempotency_key text        NOT NULL UNIQUE,
    request_digest  text        NOT NULL,
    content         jsonb       NOT NULL,
    intent_digest   text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    UNIQUE (plan_id, revision)           -- at most one live intent per plan revision
);
CREATE TRIGGER intent_immutable BEFORE UPDATE OR DELETE ON governance.execution_intent
  FOR EACH ROW EXECUTE FUNCTION workflow.reject_mutation();

-- Command idempotency (S27 §3): same key + same digest -> same result; different digest -> 409.
CREATE TABLE governance.idempotency (
    scope_key      text        PRIMARY KEY,
    request_digest text        NOT NULL,
    response       jsonb       NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE governance.outbox (
    event_id        uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type      text        NOT NULL,
    aggregate_id    text        NOT NULL,
    payload         jsonb       NOT NULL,
    payload_digest  text        NOT NULL,
    correlation_id  text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- The executor's ONLY window into governance: an authority projection it can read but
-- never write. It re-derives revocation, mode, policy currency and requester grant on each
-- read, so cached or forged copies of approval facts carry no weight (S25.2.1 step 2/3).
CREATE VIEW governance.executor_authority_v AS
SELECT
    i.intent_id,
    i.content                         AS intent,
    p.content                         AS plan,
    s.state                           AS plan_state,
    a.content                         AS approval,
    (r.approval_id IS NOT NULL)       AS approval_revoked,
    m.mode                            AS mode,
    ep.policy_digest                  AS current_policy_digest,
    EXISTS (
        SELECT 1 FROM governance.role_grant g
        WHERE g.subject_key = i.requester_key AND g.role = 'Executor' AND g.revoked_at IS NULL
          AND p.content->'target'->>'namespace' = ANY (g.namespaces)
    )                                 AS requester_grant_valid
FROM governance.execution_intent i
JOIN governance.plan_revision p ON p.plan_id = i.plan_id AND p.revision = i.revision
JOIN governance.plan_state s    ON s.plan_id = i.plan_id AND s.revision = i.revision
JOIN governance.approval a      ON a.approval_id = i.approval_id
LEFT JOIN governance.approval_revocation r ON r.approval_id = a.approval_id
LEFT JOIN governance.effective_policy ep  ON ep.workload_id = p.workload_id
CROSS JOIN governance.operating_mode m;

GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA governance TO svc_governance;
GRANT UPDATE ON governance.plan_state, governance.operating_mode, governance.effective_policy TO svc_governance;
GRANT UPDATE (revoked_at, revoked_by) ON governance.role_grant TO svc_governance;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA governance TO svc_governance;
GRANT SELECT ON governance.executor_authority_v TO svc_executor;
GRANT SELECT ON governance.outbox TO svc_executor;
GRANT SELECT ON governance.plan_revision, governance.plan_state, governance.approval, governance.execution_intent,
  governance.operating_mode TO svc_api;
REVOKE SELECT ON governance.executor_authority_v FROM svc_governance;

-- Governance reads advisory artifacts (S16 output) by reference, never writes them.
GRANT USAGE ON SCHEMA knowledge TO svc_governance;
GRANT SELECT ON knowledge.advisory_artifact TO svc_governance;
