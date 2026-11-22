-- inventory: written only by the observer (S11/S13 §4.1). Snapshots are immutable once
-- committed; a snapshot ID never points at changed content.

CREATE TABLE inventory.cluster_incarnation (
    incarnation_id   text        PRIMARY KEY,
    logical_cluster  text        NOT NULL,
    anchor_uid       text        NOT NULL,          -- kube-system namespace UID at bootstrap
    first_verified   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (logical_cluster, anchor_uid)
);

CREATE TABLE inventory.snapshot (
    snapshot_id        uuid        PRIMARY KEY,
    incarnation_id     text        NOT NULL REFERENCES inventory.cluster_incarnation (incarnation_id),
    capture_started    timestamptz NOT NULL,
    capture_completed  timestamptz NOT NULL,
    coverage           text        NOT NULL CHECK (coverage IN ('complete', 'partial', 'inconsistent', 'unavailable')),
    freshness          text        NOT NULL CHECK (freshness IN ('fresh', 'stale', 'unknown')),
    quality_flags      jsonb       NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(quality_flags) = 'array'),
    families           jsonb       NOT NULL CHECK (jsonb_typeof(families) = 'object'),
    material           jsonb       NOT NULL CHECK (jsonb_typeof(material) = 'object'),
    normalizer_rev     text        NOT NULL,
    content_digest     text        NOT NULL UNIQUE CHECK (content_digest ~ '^sha256:[0-9a-f]{64}$'),
    committed_at       timestamptz NOT NULL DEFAULT now(),
    CHECK (capture_started <= capture_completed)
);
CREATE INDEX snapshot_latest_idx ON inventory.snapshot (incarnation_id, committed_at DESC);
CREATE TRIGGER snapshot_immutable BEFORE UPDATE OR DELETE ON inventory.snapshot
  FOR EACH ROW EXECUTE FUNCTION workflow.reject_mutation();

-- Time-valid configuration revisions per exact object UID (requests/limits/scheduling).
CREATE TABLE inventory.configuration_revision (
    incarnation_id   text        NOT NULL,
    object_uid       text        NOT NULL,
    kind             text        NOT NULL,
    namespace        text        NOT NULL,
    name             text        NOT NULL,
    spec_digest      text        NOT NULL,
    spec             jsonb       NOT NULL,
    valid_from_obs   timestamptz NOT NULL,
    valid_until_obs  timestamptz NULL,
    PRIMARY KEY (incarnation_id, object_uid, spec_digest)
);

CREATE TABLE inventory.outbox (
    event_id        uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type      text        NOT NULL,
    aggregate_id    text        NOT NULL,
    payload         jsonb       NOT NULL,
    payload_digest  text        NOT NULL,
    correlation_id  text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT ON inventory.cluster_incarnation, inventory.snapshot, inventory.outbox TO svc_observer;
GRANT SELECT, INSERT, UPDATE ON inventory.configuration_revision TO svc_observer;
-- Read-only consumers (S02 §7.1): capacity analysis, API projection, governance/executor references.
GRANT SELECT ON inventory.cluster_incarnation, inventory.snapshot, inventory.configuration_revision
  TO svc_capacity, svc_api, svc_governance, svc_executor;
GRANT SELECT ON inventory.outbox TO svc_capacity;
