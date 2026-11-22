-- Narrow read projections other services need for independent authorization checks.
-- Reading current grants lets the api, capacity and ai services re-check a caller's scope
-- themselves instead of trusting the caller (S06 §2, S09 §5.1 step 6). Nobody but
-- governance may write grants or mode.

GRANT USAGE ON SCHEMA governance TO svc_capacity, svc_ai;
GRANT SELECT ON governance.role_grant, governance.operating_mode TO svc_api, svc_capacity, svc_ai;
GRANT SELECT ON governance.effective_policy TO svc_capacity, svc_api;
