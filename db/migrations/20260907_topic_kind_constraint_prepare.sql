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

  -- dev·새 baseline처럼 최종 제약이 이미 있으면 이 단계는 원장 기록만 남기는 no-op이다.
  IF v_current = v_expected_new AND v_current_valid THEN
    RETURN;
  END IF;
  IF v_current IS DISTINCT FROM v_expected_old OR NOT COALESCE(v_current_valid, false) THEN
    RAISE EXCEPTION 'unexpected messages_kind_check before topic prepare: %',
      COALESCE(v_current, '<missing>');
  END IF;

  SELECT pg_get_constraintdef(oid)
    INTO v_prepare
    FROM pg_constraint
   WHERE conrelid = 'public.messages'::regclass
     AND conname = 'messages_kind_check_topic_prepare';

  IF v_prepare IS NULL THEN
    ALTER TABLE public.messages
      ADD CONSTRAINT messages_kind_check_topic_prepare CHECK (
        kind IN ('normal','greeting','fortune_context_root','fortune_derived','topic_opening')
      ) NOT VALID;
  ELSIF v_prepare NOT IN (v_expected_new, v_expected_new || ' NOT VALID') THEN
    RAISE EXCEPTION 'unexpected prepared topic kind constraint: %', v_prepare;
  END IF;
END $$;

COMMIT;
