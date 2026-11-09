-- schema/postgres_v9_result_context_rollback.sql
--
-- DRAFT. schema/postgres_v1.sql has no rollback counterpart. This file is
-- the v9 draft rollback, retained pending independent IR diff review and
-- separate SQL execution approval. None of its SQL may run anywhere before
-- that review and approval.
--
-- Guarded behaviour, all inside ONE transaction so the lock is held through
-- commit:
--   0. The transaction pins the trusted namespace order
--      (``pg_catalog`` then ``public``) and EVERY application object below is
--      schema-qualified, so a hostile ``search_path`` or a same-name shadow
--      object can never redirect the read, lock, drops, or marker delete;
--   1. The EFFECTIVE role must hold rolsuper or rolbypassrls, checked via
--      pg_catalog.pg_roles, BEFORE any destructive DDL;
--   2. the expected public state is validated fail-closed: the object must be
--      the ordinary table created by 0009 (relkind 'r'), and the capability
--      marker must both exist AND carry exactly the version value '1' - a
--      wrong version refuses, and any other state refuses;
--   3. public.result_context is locked in ACCESS EXCLUSIVE mode BEFORE the
--      populated check; a populated table refuses, so populated original
--      results are never dropped silently and must be exported/retained
--      through an approved process first;
--   4. only an EMPTY context is removed: the trigger, function, policy,
--      table, and the capability marker. No other rows are touched, no
--      privileges are widened or revoked, nothing is disabled, and no
--      implicit dependent drops are used (dependents fail loudly instead).

BEGIN;

-- 0. Pin the namespace order for this transaction. Application objects are
--    still schema-qualified below; this prevents a hostile search_path from
--    shadowing built-ins or resolving an unqualified name by accident.
SET LOCAL search_path TO pg_catalog, public;

-- 1. Elevated effective role required before any destructive DDL.
DO $$
DECLARE
    effective_role text;
    elevated boolean;
BEGIN
    SELECT current_user INTO effective_role;
    SELECT COALESCE(rolsuper, false) OR COALESCE(rolbypassrls, false)
      INTO elevated
      FROM pg_catalog.pg_roles
     WHERE rolname = effective_role;
    IF elevated IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION
            'rollback refused: effective role % has neither rolsuper nor '
            'rolbypassrls', effective_role;
    END IF;
END $$;

-- 2. Expected public state: the ordinary 0009 table AND the capability
--    marker at exactly version 1 must both exist; the object shape must be
--    the expected ordinary table. Any deviation refuses.
DO $$
DECLARE
    table_exists boolean;
    relation_kind "char";
    marker_exists boolean;
    marker_value text;
BEGIN
    SELECT pg_catalog.to_regclass('public.result_context') IS NOT NULL
      INTO table_exists;

    SELECT cls.relkind
      INTO relation_kind
      FROM pg_catalog.pg_class cls
      JOIN pg_catalog.pg_namespace nsp ON nsp.oid = cls.relnamespace
     WHERE nsp.nspname = 'public'
       AND cls.relname = 'result_context';

    IF relation_kind IS NOT NULL AND relation_kind <> 'r' THEN
        RAISE EXCEPTION
            'rollback refused: public.result_context is not the expected '
            'ordinary table (relkind %)', relation_kind;
    END IF;

    SELECT value
      INTO marker_value
      FROM public.schema_meta
     WHERE key = 'result_context_version';
    marker_exists := FOUND;

    IF marker_exists AND marker_value IS DISTINCT FROM '1' THEN
        RAISE EXCEPTION
            'rollback refused: capability marker result_context_version '
            'must be exactly 1 (found %)',
            COALESCE(marker_value, '<null>');
    END IF;

    IF table_exists IS DISTINCT FROM marker_exists THEN
        RAISE EXCEPTION
            'rollback refused: inconsistent state between result_context '
            'and its capability marker';
    END IF;
    IF NOT table_exists THEN
        RAISE EXCEPTION
            'rollback refused: result_context and its capability marker '
            'are already absent; nothing to roll back';
    END IF;
END $$;

-- 3. Lock before the populated check; held through commit.
LOCK TABLE public.result_context IN ACCESS EXCLUSIVE MODE;

DO $$
DECLARE
    populated boolean := false;
BEGIN
    SELECT EXISTS (SELECT 1 FROM public.result_context LIMIT 1)
      INTO populated;
    IF populated THEN
        RAISE EXCEPTION
            'result_context is populated: rollback refused to preserve '
            'original results; export and retain them through an approved '
            'process first';
    END IF;
END $$;

-- 4. Empty-only removal, every object schema-qualified.
DROP TRIGGER IF EXISTS result_context_immutable ON public.result_context;
DROP FUNCTION IF EXISTS public.result_context_immutable();
DROP POLICY IF EXISTS tenant_isolation ON public.result_context;
DROP TABLE IF EXISTS public.result_context;

DELETE FROM public.schema_meta
 WHERE key = 'result_context_version'
   AND value = '1';

COMMIT;
