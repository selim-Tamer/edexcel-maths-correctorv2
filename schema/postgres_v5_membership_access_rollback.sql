-- schema/postgres_v5_membership_access_rollback.sql
--
-- Rollback for 0005. Restores the 0002 posture: no runtime membership access.
-- It does NOT delete membership rows (they are the authorization record) and
-- it does not touch any other migration's objects.

DROP POLICY IF EXISTS membership_self_read ON tenant_membership;
REVOKE SELECT (tenant_id, subject, role, active, updated_at)
    ON tenant_membership FROM marker_app;
DELETE FROM schema_meta WHERE key = 'membership_access_version';
