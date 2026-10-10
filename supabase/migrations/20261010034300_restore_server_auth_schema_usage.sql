-- Canonical open-operations: restore the existing server-owned role lookups.
-- service_role already has SELECT on account_roles and project_access.
-- No browser-role, table, policy, ownership, or row changes are made.
-- Rollback: REVOKE USAGE ON SCHEMA operations_shared FROM service_role;
GRANT USAGE ON SCHEMA operations_shared TO service_role;
