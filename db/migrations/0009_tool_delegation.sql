-- Trusted delegation lookup for AI tool calls (S09 §3.2: "cryptographically bound delegation
-- or equivalent trusted look-up"). The capacity backend may READ advisory jobs to confirm
-- that a tool call belongs to a currently leased workflow and to learn which human
-- delegated it; it can never modify or claim them.
CREATE POLICY jobs_capacity_read_ai ON workflow.jobs FOR SELECT TO svc_capacity USING (queue = 'ai');

-- S26 F08 controlled learning: the ai ingestion worker reads finalized outcomes (never writes
-- them) to create redacted `verified_outcomes` knowledge entries.
GRANT USAGE ON SCHEMA execution TO svc_ai;
GRANT SELECT ON execution.outbox, execution.outcome TO svc_ai;
