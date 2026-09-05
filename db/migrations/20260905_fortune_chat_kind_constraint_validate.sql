-- 운영 messages CHECK 확장의 2/3 단계.
-- NOT VALID로 짧게 추가한 새 제약을 쓰기와 양립 가능한 VALIDATE 단계에서 검사한다.
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
    'CHECK ((kind = ANY (ARRAY[''normal''::text, ''greeting''::text])))';
  v_expected_new constant text :=
    'CHECK ((kind = ANY (ARRAY[''normal''::text, ''greeting''::text, ''fortune_context_root''::text, ''fortune_derived''::text])))';
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
    RAISE EXCEPTION 'unexpected messages_kind_check before fortune validate: %',
      COALESCE(v_current, '<missing>');
  END IF;

  SELECT pg_get_constraintdef(oid), convalidated
    INTO v_prepare, v_prepare_valid
    FROM pg_constraint
   WHERE conrelid = 'public.messages'::regclass
     AND conname = 'messages_kind_check_fortune_prepare';
  IF v_prepare NOT IN (v_expected_new, v_expected_new || ' NOT VALID') THEN
    RAISE EXCEPTION 'fortune kind prepare constraint missing or unexpected: %',
      COALESCE(v_prepare, '<missing>');
  END IF;

  IF NOT v_prepare_valid THEN
    ALTER TABLE public.messages
      VALIDATE CONSTRAINT messages_kind_check_fortune_prepare;
  END IF;
END $$;

COMMIT;
