-- Additive new-content storage only; existing plaintext rows are not rewritten.

-- Apply before dual readers; activation is owned by the deployment controller.

BEGIN;

SET LOCAL lock_timeout = '1s';

SET LOCAL statement_timeout = '5s';

ALTER TABLE public.messages ADD COLUMN content_ciphertext bytea, ADD COLUMN content_chars integer;

ALTER TABLE public.greetings ADD COLUMN content_ciphertext bytea;

ALTER TABLE public.idempotency_keys ADD COLUMN response_ciphertext bytea;

ALTER TABLE public.messages ALTER COLUMN content DROP NOT NULL;

ALTER TABLE public.greetings ALTER COLUMN content DROP NOT NULL;

ALTER TABLE public.messages ADD CONSTRAINT messages_content_storage_ck CHECK ((content_ciphertext IS NULL AND content IS NOT NULL AND content_chars IS NULL)
 OR (content_ciphertext IS NOT NULL AND content IS NULL AND content_chars IS NOT NULL
     AND content_chars >= 0 AND content_chars <= 1048576
     AND CASE WHEN octet_length(content_ciphertext) BETWEEN 32 AND 1048608 THEN
         substring(content_ciphertext FROM 1 FOR 2) = decode('0100','hex')
         AND (get_byte(content_ciphertext,2) > 0 OR get_byte(content_ciphertext,3) > 0)
         AND content_chars <= octet_length(content_ciphertext)-32
         AND octet_length(content_ciphertext)-32 <= content_chars*4
         ELSE false END)) NOT VALID;

ALTER TABLE public.greetings ADD CONSTRAINT greetings_content_storage_ck CHECK ((content_ciphertext IS NULL AND content IS NOT NULL)
 OR (content_ciphertext IS NOT NULL AND content IS NULL
     AND CASE WHEN octet_length(content_ciphertext) BETWEEN 32 AND 1048608 THEN
         substring(content_ciphertext FROM 1 FOR 2) = decode('0100','hex')
         AND (get_byte(content_ciphertext,2) > 0 OR get_byte(content_ciphertext,3) > 0)
         ELSE false END)) NOT VALID;

ALTER TABLE public.idempotency_keys ADD CONSTRAINT idempotency_response_storage_ck CHECK ((response_schema_version <> 3 AND response_ciphertext IS NULL)
 OR (response_schema_version = 3 AND
     ((response IS NULL AND response_ciphertext IS NULL
       AND (redacted_at IS NOT NULL OR terminal_status IN ('expired','redacted')))
      OR (response IS NOT NULL AND response = '{}'::jsonb AND response_ciphertext IS NOT NULL
          AND redacted_at IS NULL AND terminal_status = 'succeeded'
          AND request_hash IS NOT NULL AND request_hash <> ''
          AND key NOT LIKE 'shop-purchase:%' AND key NOT LIKE 'topic-prepare:%'
          AND CASE WHEN octet_length(response_ciphertext) BETWEEN 32 AND 4194336 THEN
              substring(response_ciphertext FROM 1 FOR 2) = decode('0100','hex')
              AND (get_byte(response_ciphertext,2) > 0 OR get_byte(response_ciphertext,3) > 0)
              ELSE false END)))) NOT VALID;

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
  SET response = NULL, response_ciphertext = NULL, terminal_status = 'redacted', redacted_at = now()
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

INSERT INTO public.app_config(key,value) VALUES ('chat_content_encryption_state',
    '{"started":false,"active_key_id":1,"format_version":1,"required_key_ids":[1]}'::jsonb)
ON CONFLICT (key) DO NOTHING;

COMMIT;

