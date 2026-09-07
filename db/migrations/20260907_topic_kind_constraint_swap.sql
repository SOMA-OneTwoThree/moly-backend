-- Topic opening kind: apply prepare -> validate -> swap before enabling topic chat.
-- Preserve the existing fortune kinds and keep the table-scan lock out of the swap.
BEGIN;

SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '15s';

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
    IF EXISTS (
      SELECT 1 FROM pg_constraint
       WHERE conrelid = 'public.messages'::regclass
         AND conname = 'messages_kind_check_topic_prepare'
    ) THEN
      RAISE EXCEPTION 'final messages kind constraint exists with stale prepare constraint';
    END IF;
    RETURN;
  END IF;
  IF v_current IS DISTINCT FROM v_expected_old OR NOT COALESCE(v_current_valid, false) THEN
    RAISE EXCEPTION 'unexpected messages_kind_check before topic swap: %',
      COALESCE(v_current, '<missing>');
  END IF;

  SELECT pg_get_constraintdef(oid), convalidated
    INTO v_prepare, v_prepare_valid
    FROM pg_constraint
   WHERE conrelid = 'public.messages'::regclass
     AND conname = 'messages_kind_check_topic_prepare';
  IF v_prepare IS DISTINCT FROM v_expected_new OR NOT COALESCE(v_prepare_valid, false) THEN
    RAISE EXCEPTION 'topic kind prepare constraint is not validated: % / %',
      COALESCE(v_prepare, '<missing>'), COALESCE(v_prepare_valid::text, '<missing>');
  END IF;

  ALTER TABLE public.messages DROP CONSTRAINT messages_kind_check;
  ALTER TABLE public.messages
    RENAME CONSTRAINT messages_kind_check_topic_prepare TO messages_kind_check;
END $$;

COMMIT;
