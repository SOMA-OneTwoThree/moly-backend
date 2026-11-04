-- 최신 운영 main64 위 가드레일/대화 맥락 도입용 additive 구조 변경.
-- 사용자 원문·기억·일기·기존 요약을 수정하거나 삭제하지 않는다.
-- 두 개발 검증 SQL의 실행문을 합친 릴리스용 묶음이다.
-- 실행은 db.apply의 단일 트랜잭션/lock_timeout 2초/statement_timeout 60초를 사용한다.
-- 운영 사전 metadata 대조 → 전체 점검/기존 writer drain → dry-run → 검증한 SHA로 commit 순서.
-- 실패하면 전체 rollback. 복구 때 새 종류·컬럼을 지우지 않는다.


DO $guardrails$
DECLARE
  existing_definition text;
  existing_kinds text[];
BEGIN
  SELECT pg_get_constraintdef(c.oid)
    INTO existing_definition
    FROM pg_constraint c
   WHERE c.conrelid = 'public.messages'::regclass
     AND c.conname = 'messages_kind_check'
     AND c.contype = 'c';
  IF existing_definition IS NULL THEN
    RAISE EXCEPTION 'messages_kind_check is missing; inspect the target schema first';
  END IF;
  SELECT array_agg(DISTINCT parts[1] ORDER BY parts[1])
    INTO existing_kinds
    FROM regexp_matches(existing_definition, '''([^'']+)''', 'g') AS parts;
  IF existing_kinds IS DISTINCT FROM ARRAY[
       'fortune_context_root', 'fortune_derived', 'greeting', 'normal', 'topic_opening'
     ]::text[]
     AND existing_kinds IS DISTINCT FROM ARRAY[
       'fortune_context_root', 'fortune_derived', 'greeting', 'normal',
       'safety', 'safety_check', 'topic_opening'
     ]::text[] THEN
    RAISE EXCEPTION 'Unexpected message kinds; preserve and review the existing constraint';
  END IF;
END
$guardrails$;

ALTER TABLE public.messages
  ADD CONSTRAINT messages_kind_guardrails_check
  CHECK (kind IN (
    'normal', 'greeting', 'fortune_context_root', 'fortune_derived', 'topic_opening',
    'safety_check', 'safety'
  )) NOT VALID;
ALTER TABLE public.messages VALIDATE CONSTRAINT messages_kind_guardrails_check;
ALTER TABLE public.messages DROP CONSTRAINT messages_kind_check;
ALTER TABLE public.messages
  RENAME CONSTRAINT messages_kind_guardrails_check TO messages_kind_check;

ALTER TABLE public.conversation_checkpoints
  ADD COLUMN IF NOT EXISTS state_json jsonb,
  ADD COLUMN IF NOT EXISTS validation_json jsonb;
ALTER TABLE public.mem0_memory_registry
  ADD COLUMN IF NOT EXISTS canonical_text text,
  ADD COLUMN IF NOT EXISTS context_priority text NOT NULL DEFAULT 'related';
ALTER TABLE public.mem0_ingest_candidates
  ADD COLUMN IF NOT EXISTS context_priority text NOT NULL DEFAULT 'related';
DO $context_priority$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'public.mem0_memory_registry'::regclass
       AND conname = 'mem0_memory_registry_context_priority_check'
  ) THEN
    ALTER TABLE public.mem0_memory_registry
      ADD CONSTRAINT mem0_memory_registry_context_priority_check
      CHECK (context_priority IN ('related', 'required')) NOT VALID;
  END IF;
END
$context_priority$;
ALTER TABLE public.mem0_memory_registry
  VALIDATE CONSTRAINT mem0_memory_registry_context_priority_check;
DO $candidate_priority$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'public.mem0_ingest_candidates'::regclass
       AND conname = 'mem0_ingest_candidates_context_priority_check'
  ) THEN
    ALTER TABLE public.mem0_ingest_candidates
      ADD CONSTRAINT mem0_ingest_candidates_context_priority_check
      CHECK (context_priority IN ('related', 'required')) NOT VALID;
  END IF;
END
$candidate_priority$;
ALTER TABLE public.mem0_ingest_candidates
  VALIDATE CONSTRAINT mem0_ingest_candidates_context_priority_check;
ALTER TABLE public.chat_contexts
  ADD COLUMN IF NOT EXISTS contract_processed_through_message_id bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS contract_generation bigint NOT NULL DEFAULT 0;



ALTER TABLE public.chat_contexts
  ADD COLUMN IF NOT EXISTS retention_pressure_through_message_id bigint;
DO $pressure_constraint$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
    WHERE conrelid='public.chat_contexts'::regclass
      AND conname='chat_contexts_retention_pressure_positive_check') THEN
    ALTER TABLE public.chat_contexts
      ADD CONSTRAINT chat_contexts_retention_pressure_positive_check
      CHECK (retention_pressure_through_message_id IS NULL OR retention_pressure_through_message_id>0)
      NOT VALID;
  END IF;
END
$pressure_constraint$;
ALTER TABLE public.chat_contexts VALIDATE CONSTRAINT chat_contexts_retention_pressure_positive_check;

CREATE OR REPLACE FUNCTION public.reset_retention_pressure_generation() RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $function$
BEGIN
  IF NEW.memory_generation IS DISTINCT FROM OLD.memory_generation THEN
    NEW.retention_pressure_through_message_id := NULL;
  END IF;
  RETURN NEW;
END;
$function$;
REVOKE ALL ON FUNCTION public.reset_retention_pressure_generation() FROM PUBLIC, anon, authenticated, service_role;
DROP TRIGGER IF EXISTS chat_contexts_retention_pressure_generation ON public.chat_contexts;
CREATE TRIGGER chat_contexts_retention_pressure_generation BEFORE UPDATE OF memory_generation ON public.chat_contexts
FOR EACH ROW EXECUTE FUNCTION public.reset_retention_pressure_generation();

CREATE OR REPLACE FUNCTION public.reset_retention_pressure_privacy() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO ''
AS $function$
BEGIN
  IF NEW.state <> 'active' OR (TG_OP = 'UPDATE' AND NEW.epoch IS DISTINCT FROM OLD.epoch) THEN
    UPDATE public.chat_contexts SET retention_pressure_through_message_id = NULL
    WHERE user_id = NEW.user_id AND retention_pressure_through_message_id IS NOT NULL;
  END IF;
  RETURN NEW;
END;
$function$;
REVOKE ALL ON FUNCTION public.reset_retention_pressure_privacy() FROM PUBLIC, anon, authenticated, service_role;
DROP TRIGGER IF EXISTS privacy_subject_barriers_retention_pressure ON public.privacy_subject_barriers;
CREATE TRIGGER privacy_subject_barriers_retention_pressure AFTER INSERT OR UPDATE OF state, epoch ON public.privacy_subject_barriers
FOR EACH ROW EXECUTE FUNCTION public.reset_retention_pressure_privacy();
