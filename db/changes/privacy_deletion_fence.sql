-- Account deletion fence RPCs for moly-auth DELETE /me (release handoff, apply with db.apply).
-- Apply BEFORE the backend image whose schema contract lists these functions and before moly-auth
-- calls them. Additive: nothing runs them until moly-auth does, and an older backend image's
-- contract accepts extra functions, so its rollback still passes the deploy preflight.
-- Expected: catalog-only changes, no table locks beyond the function definitions.
-- Rollback: release moly-auth without the calls and a backend image without them in its contract,
-- then DROP FUNCTION public.begin_subject_deletion(uuid, uuid), public.abort_subject_deletion(uuid, uuid);
-- BEGIN/COMMIT keep SET LOCAL effective under plain psql; db.apply strips them and uses its own transaction.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
DO $$
BEGIN
  -- abort and the backend sweep read auth.users to tell a live account from a deleted one.
  IF NOT has_table_privilege('auth.users', 'SELECT') THEN
    RAISE EXCEPTION 'this role cannot read auth.users; no functions created';
  END IF;
END;
$$;
CREATE OR REPLACE FUNCTION public.begin_subject_deletion(p_user_id uuid, p_operation_id uuid)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
  v_watermark bigint;
BEGIN
  IF p_user_id IS NULL OR p_operation_id IS NULL THEN
    RAISE EXCEPTION 'user and operation are required';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  -- Every deletion attempt is a new epoch; work queued in an earlier epoch is rejected.
  INSERT INTO public.privacy_subject_barriers AS b (user_id, state, operation_id, high_watermark)
  SELECT p_user_id, 'deleting', p_operation_id, c.memory_source_watermark
  FROM public.chat_contexts c WHERE c.user_id = p_user_id
  ON CONFLICT (user_id) DO UPDATE SET
    state = 'deleting', operation_id = EXCLUDED.operation_id, epoch = b.epoch + 1,
    high_watermark = GREATEST(b.high_watermark, EXCLUDED.high_watermark), updated_at = now()
  RETURNING b.high_watermark INTO v_watermark;
  IF NOT FOUND THEN
    -- An account that never chatted has no chat context.
    INSERT INTO public.privacy_subject_barriers AS b (user_id, state, operation_id, high_watermark)
    VALUES (p_user_id, 'deleting', p_operation_id, 0)
    ON CONFLICT (user_id) DO UPDATE SET
      state = 'deleting', operation_id = EXCLUDED.operation_id, epoch = b.epoch + 1,
      updated_at = now();
    v_watermark := 0;
  END IF;

  DELETE FROM public.chat_topic_entries WHERE user_id = p_user_id;
  DELETE FROM public.user_topic_states WHERE user_id = p_user_id;
  UPDATE public.idempotency_keys
  SET response = NULL, terminal_status = 'redacted', redacted_at = now()
  WHERE user_id = p_user_id;
  UPDATE public.chat_response_references
  SET state = 'unavailable', diary_id = NULL, rendered_metadata = '{}'::jsonb,
      redacted_at = now(), redaction_reason = 'subject_deleting'
  WHERE user_id = p_user_id;
  UPDATE public.async_jobs
  SET state = CASE WHEN state = 'ready' THEN 'cancelled' ELSE state END,
      payload = '{}'::jsonb, result_detail = NULL, payload_redacted_at = now(),
      finished_at = CASE WHEN state = 'ready' THEN now() ELSE finished_at END,
      result_code = CASE WHEN state = 'ready' THEN 'subject_deleting' ELSE result_code END
  WHERE user_id = p_user_id AND state IN ('ready', 'running', 'succeeded', 'dead', 'cancelled');

  INSERT INTO public.privacy_ledger_events(operation_id, user_id, event, high_watermark)
  VALUES (p_operation_id, p_user_id, 'serving_blocked_and_redacted', v_watermark);
  RETURN v_watermark;
END;
$$;

-- Undo begin_subject_deletion when the account deletion itself failed. Only while the account
-- exists: once it is gone the barrier belongs to the sweep. The epoch returns to the memory
-- pipeline's (0 without a pipeline, the epoch enrollment starts at), otherwise every later
-- memory job for the account is rejected as a stale epoch.
-- Redacted copies and cancelled jobs are not restored.
CREATE OR REPLACE FUNCTION public.abort_subject_deletion(p_user_id uuid, p_operation_id uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  UPDATE public.privacy_subject_barriers b
  SET state = 'active', operation_id = NULL, updated_at = now(),
      epoch = COALESCE(
        (SELECT s.privacy_epoch FROM public.memory_pipeline_states s WHERE s.user_id = b.user_id),
        0)
  WHERE b.user_id = p_user_id AND b.operation_id = p_operation_id AND b.state = 'deleting'
    AND EXISTS (SELECT 1 FROM auth.users u WHERE u.id = b.user_id);
  IF NOT FOUND THEN
    RETURN false;
  END IF;
  INSERT INTO public.privacy_ledger_events(operation_id, user_id, event, high_watermark)
  VALUES (p_operation_id, p_user_id, 'deletion_aborted', NULL);
  RETURN true;
END;
$$;

REVOKE ALL ON FUNCTION public.begin_subject_deletion(uuid, uuid) FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.begin_subject_deletion(uuid, uuid) TO service_role;
REVOKE ALL ON FUNCTION public.abort_subject_deletion(uuid, uuid) FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.abort_subject_deletion(uuid, uuid) TO service_role;
DO $$
BEGIN
  IF has_function_privilege('anon', 'public.begin_subject_deletion(uuid,uuid)', 'EXECUTE')
    OR has_function_privilege('authenticated', 'public.begin_subject_deletion(uuid,uuid)', 'EXECUTE')
    OR has_function_privilege('anon', 'public.abort_subject_deletion(uuid,uuid)', 'EXECUTE')
    OR has_function_privilege('authenticated', 'public.abort_subject_deletion(uuid,uuid)', 'EXECUTE')
    OR NOT has_function_privilege('service_role', 'public.begin_subject_deletion(uuid,uuid)', 'EXECUTE')
    OR NOT has_function_privilege('service_role', 'public.abort_subject_deletion(uuid,uuid)', 'EXECUTE') THEN
    RAISE EXCEPTION 'unexpected deletion fence grants; rolled back';
  END IF;
END;
$$;
-- PostgREST must see the new functions before moly-auth calls them.
NOTIFY pgrst, 'reload schema';
COMMIT;
-- Validate with the regenerated schema contract (uv run python -m db.verify --env <env>).
