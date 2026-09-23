-- Narrow repair for the unintended Supabase default EXECUTE grant.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '10s';
DO $$
DECLARE v_oid regprocedure := to_regprocedure('public.cleanup_unowned_subscription()');
BEGIN
  IF v_oid IS NULL OR md5(pg_get_functiondef(v_oid)) IS DISTINCT FROM 'cbd1bd66ca4e52f4754ebaa100378f71' THEN
    RAISE EXCEPTION 'unexpected cleanup function definition; no ACL changes applied';
  END IF;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.cleanup_unowned_subscription() FROM service_role;
DO $$
BEGIN
  IF has_function_privilege('anon', 'public.cleanup_unowned_subscription()', 'EXECUTE')
    OR has_function_privilege('authenticated', 'public.cleanup_unowned_subscription()', 'EXECUTE')
    OR has_function_privilege('service_role', 'public.cleanup_unowned_subscription()', 'EXECUTE') THEN
    RAISE EXCEPTION 'cleanup function remains callable; ACL repair rolled back';
  END IF;
END;
$$;
COMMIT;

SELECT
  md5(pg_get_functiondef('public.cleanup_unowned_subscription()'::regprocedure)) = 'cbd1bd66ca4e52f4754ebaa100378f71' AS definition_unchanged,
  NOT has_function_privilege('anon', 'public.cleanup_unowned_subscription()', 'EXECUTE') AS anon_blocked,
  NOT has_function_privilege('authenticated', 'public.cleanup_unowned_subscription()', 'EXECUTE') AS authenticated_blocked,
  NOT has_function_privilege('service_role', 'public.cleanup_unowned_subscription()', 'EXECUTE') AS service_role_blocked;
