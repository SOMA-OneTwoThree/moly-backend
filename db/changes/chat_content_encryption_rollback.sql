-- PRE-ACTIVATION ONLY. Ciphertext adoption makes rollback to old readers forbidden.
BEGIN;
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '5s';
DO $rollback$
DECLARE v_state jsonb;
BEGIN
  SELECT value INTO v_state FROM public.app_config
  WHERE key = 'chat_content_encryption_state' FOR UPDATE;
  IF v_state IS NULL OR (v_state->>'started')::boolean IS DISTINCT FROM false THEN
    RAISE EXCEPTION 'content_encryption_rollback_forbidden';
  END IF;
  LOCK TABLE public.messages, public.greetings, public.idempotency_keys
    IN ACCESS EXCLUSIVE MODE NOWAIT;
  IF EXISTS (SELECT 1 FROM public.messages WHERE content_ciphertext IS NOT NULL OR content IS NULL)
     OR EXISTS (SELECT 1 FROM public.greetings WHERE content_ciphertext IS NOT NULL OR content IS NULL)
     OR EXISTS (SELECT 1 FROM public.idempotency_keys WHERE response_ciphertext IS NOT NULL OR response_schema_version=3) THEN
    RAISE EXCEPTION 'content_encryption_rollback_forbidden';
  END IF;
END;
$rollback$;
ALTER TABLE public.messages DROP CONSTRAINT messages_content_storage_ck;
ALTER TABLE public.greetings DROP CONSTRAINT greetings_content_storage_ck;
ALTER TABLE public.idempotency_keys DROP CONSTRAINT idempotency_response_storage_ck;
ALTER TABLE public.messages ALTER COLUMN content SET NOT NULL;
ALTER TABLE public.greetings ALTER COLUMN content SET NOT NULL;
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
ALTER TABLE public.messages DROP COLUMN content_ciphertext, DROP COLUMN content_chars;
ALTER TABLE public.greetings DROP COLUMN content_ciphertext;
ALTER TABLE public.idempotency_keys DROP COLUMN response_ciphertext;
COMMIT;
