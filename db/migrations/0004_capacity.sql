-- capacity: registered queries, qualified observations, forecasts, candidates and
-- evaluations (S12/S13/S14/S19-S22). Written only by the capacity service. Finalized
-- records are immutable; a correction is a new record that supersedes the old one.

CREATE TABLE capacity.query_definition (
    query_id          text        NOT NULL,
    revision          integer     NOT NULL CHECK (revision > 0),
    expression_digest text        NOT NULL CHECK (expression_digest ~ '^sha256:[0-9a-f]{64}$'),
    unit              text        NOT NULL,
    definition        jsonb       NOT NULL CHECK (jsonb_typeof(definition) = 'object'),
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (query_id, revision)
);
CREATE TRIGGER query_definition_immutable BEFORE UPDATE OR DELETE ON capacity.query_definition
  FOR EACH ROW EXECUTE FUNCTION workflow.reject_mutation();

CREATE TABLE capacity.observation_window (
    observation_id       uuid        PRIMARY KEY,
    cluster_incarnation  text        NOT NULL,
    namespace            text        NOT NULL,
    workload_id          text        NOT NULL,
    query_id             text        NOT NULL,
    query_revision       integer     NOT NULL,
    window_start         timestamptz NOT NULL,
    window_end           timestamptz NOT NULL,
    step_seconds         integer     NOT NULL CHECK (step_seconds > 0),
    measurement_origin   text        NOT NULL CHECK (measurement_origin IN ('observed_live', 'historical_observed')),
    workload_origin      text        NOT NULL CHECK (workload_origin IN ('scenario_generated', 'baseline_lab', 'external_or_unknown')),
    quality              jsonb       NOT NULL CHECK (jsonb_typeof(quality) = 'object'),
    summary              jsonb       NOT NULL CHECK (jsonb_typeof(summary) = 'object'),
    inventory_refs       jsonb       NOT NULL CHECK (jsonb_typeof(inventory_refs) = 'array'),
    result_digest        text        NOT NULL CHECK (result_digest ~ '^sha256:[0-9a-f]{64}$'),
    committed_at         timestamptz NOT NULL DEFAULT now(),
    CHECK (window_start < window_end),
    FOREIGN KEY (query_id, query_revision) REFERENCES capacity.query_definition (query_id, revision)
);
CREATE INDEX obs_target_window_idx ON capacity.observation_window
    (cluster_incarnation, namespace, workload_id, window_end DESC);
CREATE TRIGGER observation_immutable BEFORE UPDATE OR DELETE ON capacity.observation_window
  FOR EACH ROW EXECUTE FUNCTION workflow.reject_mutation();

-- A forecast run is finalized as either 'available' or 'refused'; both are immutable facts.
CREATE TABLE capacity.forecast_run (
    forecast_id         text        PRIMARY KEY CHECK (forecast_id ~ '^sha256:[0-9a-f]{64}$'),
    idempotency_key     text        NOT NULL UNIQUE,
    input_digest        text        NOT NULL,
    cluster_incarnation text        NOT NULL,
    namespace           text        NOT NULL,
    workload_id         text        NOT NULL,
    resource            text        NOT NULL,
    horizon             text        NOT NULL CHECK (horizon IN ('1h', '24h')),
    status              text        NOT NULL CHECK (status IN ('available', 'refused', 'not_applicable')),
    policy_revision     text        NOT NULL,
    model_family        text        NULL,
    reason_codes        jsonb       NOT NULL DEFAULT '[]'::jsonb,
    result              jsonb       NOT NULL,
    produced_at         timestamptz NOT NULL DEFAULT now(),
    CHECK (status <> 'available' OR model_family IS NOT NULL)
);
CREATE INDEX forecast_target_idx ON capacity.forecast_run (cluster_incarnation, workload_id, resource, horizon, produced_at DESC);
CREATE TRIGGER forecast_immutable BEFORE UPDATE OR DELETE ON capacity.forecast_run
  FOR EACH ROW EXECUTE FUNCTION workflow.reject_mutation();

CREATE TABLE capacity.analysis (
    analysis_digest  text        PRIMARY KEY CHECK (analysis_digest ~ '^sha256:[0-9a-f]{64}$'),
    target_key       text        NOT NULL,
    workload_id      text        NOT NULL,
    namespace        text        NOT NULL,
    policy_digest    text        NOT NULL,
    status           text        NOT NULL,
    result           jsonb       NOT NULL,
    produced_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER analysis_immutable BEFORE UPDATE OR DELETE ON capacity.analysis
  FOR EACH ROW EXECUTE FUNCTION workflow.reject_mutation();

CREATE TABLE capacity.candidate (
    candidate_digest text        PRIMARY KEY CHECK (candidate_digest ~ '^sha256:[0-9a-f]{64}$'),
    analysis_digest  text        NOT NULL REFERENCES capacity.analysis (analysis_digest),
    namespace        text        NOT NULL,
    workload_id      text        NOT NULL,
    content          jsonb       NOT NULL,
    produced_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE capacity.evaluation (
    integrity_digest text        PRIMARY KEY CHECK (integrity_digest ~ '^sha256:[0-9a-f]{64}$'),
    candidate_digest text        NOT NULL REFERENCES capacity.candidate (candidate_digest),
    analysis_digest  text        NOT NULL REFERENCES capacity.analysis (analysis_digest),
    status           text        NOT NULL CHECK (status IN ('qualified', 'rejected', 'indeterminate')),
    rank             integer     NULL,
    content          jsonb       NOT NULL,
    produced_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER candidate_immutable BEFORE UPDATE OR DELETE ON capacity.candidate
  FOR EACH ROW EXECUTE FUNCTION workflow.reject_mutation();
CREATE TRIGGER evaluation_immutable BEFORE UPDATE OR DELETE ON capacity.evaluation
  FOR EACH ROW EXECUTE FUNCTION workflow.reject_mutation();

-- S05 experiment bundles registered by digest; a different digest for the same run is a conflict.
CREATE TABLE capacity.experiment_registration (
    registration_id     uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    producer            text        NOT NULL,
    experiment_run_id   text        NOT NULL,
    cluster_incarnation text        NOT NULL,
    manifest_digest     text        NOT NULL CHECK (manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
    scenario_id         text        NOT NULL,
    scenario_revision   integer     NOT NULL CHECK (scenario_revision > 0),
    seed                bigint      NOT NULL,
    baseline_class      text        NULL CHECK (baseline_class IN ('B0', 'B1', 'B2', 'B3')),
    manifest            jsonb       NOT NULL,
    registered_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (producer, experiment_run_id)
);

-- Reference-aware retention pins (S13 §8.1): pinned evidence cannot be purged.
CREATE TABLE capacity.evidence_pin (
    owner_domain   text        NOT NULL,
    owner_record   text        NOT NULL,
    evidence_ref   text        NOT NULL,
    pin_reason     text        NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    released_at    timestamptz NULL,
    PRIMARY KEY (owner_domain, owner_record, evidence_ref)
);

CREATE TABLE capacity.outbox (
    event_id        uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type      text        NOT NULL,
    aggregate_id    text        NOT NULL,
    payload         jsonb       NOT NULL,
    payload_digest  text        NOT NULL,
    correlation_id  text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA capacity TO svc_capacity;
GRANT UPDATE (released_at) ON capacity.evidence_pin TO svc_capacity;
GRANT SELECT ON capacity.query_definition, capacity.observation_window, capacity.forecast_run,
  capacity.analysis, capacity.candidate, capacity.evaluation, capacity.experiment_registration
  TO svc_api, svc_governance, svc_ai;
GRANT SELECT ON capacity.outbox TO svc_governance;
