-- Topic opening kind: apply prepare -> validate -> swap before enabling topic chat.
-- Preserve the existing fortune kinds and keep the table-scan lock out of the swap.
BEGIN;

SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';

DO $$
DECLARE
  v_current text;
  v_current_valid boolean;
  v_prepare text;
  v_prepare_valid boolean;
  v_expected_old constant text :=
    'CHECK ((kind = ANY (ARRAY[''normal''::text, ''greeting''::text, ''fortune_context_root''::text, ''fortune_derived''::text])))';
  v_expected_new constant text :=
    'CHECK ((kind = ANY (ARRAY[''normal''::text, ''greeting''::text, ''fortune_context_root''::text, ''fortune_derived''::text, ''topic_opening''::text])))';
BEGIN
  SELECT pg_get_constraintdef(oid), convalidated
    INTO v_current, v_current_valid
    FROM pg_constraint
   WHERE conrelid = 'public.messages'::regclass
     AND conname = 'messages_kind_check';

  IF v_current = v_expected_new AND v_current_valid THEN
    RETURN;
  END IF;
  IF v_current IS DISTINCT FROM v_expected_old OR NOT COALESCE(v_current_valid, false) THEN
    RAISE EXCEPTION 'unexpected messages_kind_check before topic validate: %',
      COALESCE(v_current, '<missing>');
  END IF;

  SELECT pg_get_constraintdef(oid), convalidated
    INTO v_prepare, v_prepare_valid
    FROM pg_constraint
   WHERE conrelid = 'public.messages'::regclass
     AND conname = 'messages_kind_check_topic_prepare';
  IF v_prepare NOT IN (v_expected_new, v_expected_new || ' NOT VALID') THEN
    RAISE EXCEPTION 'topic kind prepare constraint missing or unexpected: %',
      COALESCE(v_prepare, '<missing>');
  END IF;

  IF NOT v_prepare_valid THEN
    ALTER TABLE public.messages
      VALIDATE CONSTRAINT messages_kind_check_topic_prepare;
  END IF;
END $$;

COMMIT;
