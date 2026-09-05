-- 운영 messages CHECK 확장의 3/3 단계.
-- 검증이 끝난 제약만 짧은 ACCESS EXCLUSIVE 구간에서 기존 이름으로 교체한다.
-- 2초 안에 lock을 얻지 못하면 요청을 줄 세우지 않고 전체 rollback하므로 잠시 뒤 재시도한다.
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
    IF EXISTS (
      SELECT 1 FROM pg_constraint
       WHERE conrelid = 'public.messages'::regclass
         AND conname = 'messages_kind_check_fortune_prepare'
    ) THEN
      RAISE EXCEPTION 'final messages kind constraint exists with stale prepare constraint';
    END IF;
    RETURN;
  END IF;
  IF v_current IS DISTINCT FROM v_expected_old OR NOT COALESCE(v_current_valid, false) THEN
    RAISE EXCEPTION 'unexpected messages_kind_check before fortune swap: %',
      COALESCE(v_current, '<missing>');
  END IF;

  SELECT pg_get_constraintdef(oid), convalidated
    INTO v_prepare, v_prepare_valid
    FROM pg_constraint
   WHERE conrelid = 'public.messages'::regclass
     AND conname = 'messages_kind_check_fortune_prepare';
  IF v_prepare IS DISTINCT FROM v_expected_new OR NOT COALESCE(v_prepare_valid, false) THEN
    RAISE EXCEPTION 'fortune kind prepare constraint is not validated: % / %',
      COALESCE(v_prepare, '<missing>'), COALESCE(v_prepare_valid::text, '<missing>');
  END IF;

  ALTER TABLE public.messages DROP CONSTRAINT messages_kind_check;
  ALTER TABLE public.messages
    RENAME CONSTRAINT messages_kind_check_fortune_prepare TO messages_kind_check;
END $$;

COMMIT;
