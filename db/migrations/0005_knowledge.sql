-- knowledge: S18 operational knowledge base (four collections) and S16 advisory artifacts.
-- Written only by the ai service. Content is redacted BEFORE it is chunked or embedded;
-- no Restricted classification is ever stored. Retrieval applies scope/classification/time
-- filters in SQL before lexical or vector ranking, so foreign documents never reach ranking.

CREATE TABLE knowledge.source_document (
    document_id      text        NOT NULL,
    revision         integer     NOT NULL CHECK (revision > 0),
    collection       text        NOT NULL CHECK (collection IN ('runbooks', 'incidents', 'configuration_history',
                                                                'verified_outcomes')),
    title            text        NOT NULL,
    cluster_id       text        NOT NULL,
    namespace_scope  text        NOT NULL,          -- a namespace or '*cluster*' for cluster-wide docs
    workload_id      text        NULL,
    classification   text        NOT NULL CHECK (classification IN ('Public', 'Internal', 'Confidential')),
    source_kind      text        NOT NULL,
    ingest_principal text        NOT NULL,
    content_digest   text        NOT NULL CHECK (content_digest ~ '^sha256:[0-9a-f]{64}$'),
    redaction_rev    text        NOT NULL,
    valid_from       timestamptz NOT NULL,
    valid_to         timestamptz NULL,
    state            text        NOT NULL CHECK (state IN ('active', 'superseded', 'revoked', 'quarantined')),
    synthetic_origin boolean     NOT NULL DEFAULT false,
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, revision)
);

CREATE TABLE knowledge.chunk (
    chunk_id         text        PRIMARY KEY,
    document_id      text        NOT NULL,
    revision         integer     NOT NULL,
    ordinal          integer     NOT NULL CHECK (ordinal >= 0),
    char_start       integer     NOT NULL,
    char_end         integer     NOT NULL,
    content          text        NOT NULL,
    content_tsv      tsvector    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    embedding        vector      NULL,             -- NULL when embeddings are unavailable (lexical-only)
    embedding_model  text        NULL,
    chunker_rev      text        NOT NULL,
    FOREIGN KEY (document_id, revision) REFERENCES knowledge.source_document (document_id, revision),
    UNIQUE (document_id, revision, ordinal)
);
CREATE INDEX chunk_tsv_idx ON knowledge.chunk USING gin (content_tsv);

CREATE TABLE knowledge.advisory_artifact (
    artifact_digest  text        PRIMARY KEY CHECK (artifact_digest ~ '^sha256:[0-9a-f]{64}$'),
    workflow_run_id  text        NOT NULL,
    mode             text        NOT NULL CHECK (mode IN ('diagnostic', 'optimization_review')),
    status           text        NOT NULL CHECK (status IN ('completed', 'incomplete', 'unavailable', 'rejected')),
    namespace        text        NOT NULL,
    workload_id      text        NOT NULL,
    content          jsonb       NOT NULL,
    produced_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER advisory_immutable BEFORE UPDATE OR DELETE ON knowledge.advisory_artifact
  FOR EACH ROW EXECUTE FUNCTION workflow.reject_mutation();

-- Durable S16 workflow checkpoints (lease/fencing lives on the workflow.jobs row).
CREATE TABLE knowledge.workflow_checkpoint (
    workflow_run_id  text        NOT NULL,
    stage            text        NOT NULL,
    attempt          integer     NOT NULL,
    content          jsonb       NOT NULL,
    content_digest   text        NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workflow_run_id, stage, attempt)
);

CREATE TABLE knowledge.outbox (
    event_id        uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type      text        NOT NULL,
    aggregate_id    text        NOT NULL,
    payload         jsonb       NOT NULL,
    payload_digest  text        NOT NULL,
    correlation_id  text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA knowledge TO svc_ai;
GRANT UPDATE (state, valid_to) ON knowledge.source_document TO svc_ai;
GRANT UPDATE (embedding, embedding_model) ON knowledge.chunk TO svc_ai;
GRANT SELECT ON knowledge.advisory_artifact TO svc_api;
