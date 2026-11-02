-- 기억·요약·위기 가드레일의 기존 환경 차이. 검토용이며 자동 적용하지 않는다.
-- 대상: 부모 작업자가 연결 대상 프로젝트를 확인한 기존 개발 Supabase만.
-- 운영 DB에는 적용하지 않는다. 운영 반영이 필요하면 별도 검토·승인 절차를 따른다.
-- db.apply의 단일 트랜잭션 안에서 실행한다(기본 rollback, 승인된 dev만 --commit).
-- lock_timeout 2초/statement_timeout 60초를 유지하며 잠금 실패 시 제한을 늘리지 않는다.
-- 영향: messages의 허용 종류 확장, nullable 파생 컬럼/기본값 있는 커서 컬럼 추가.
-- 본문·종류·기억·기존 checkpoint 행을 갱신하거나 삭제하지 않는다.

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

-- 새 CHECK를 검증한 뒤 기존 이름으로 바꾼다. 트랜잭션 밖에 무제약 상태를 노출하지 않는다.
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

-- 적용 후 확인(읽기 전용):
-- 1. pg_constraint에서 messages_kind_check와 context_priority CHECK의 convalidated=true,
--    pg_get_constraintdef 내용을 확인한다. 기존 다섯 종류와 새 두 종류가 모두 허용돼야 한다.
-- 2. 새 컬럼의 타입/NULL/default를 schema.sql·ORM과 대조한다. IF NOT EXISTS만으로 정상 판정 금지.
-- 3. parent가 db/schema_contract.json을 개발 구조에서 정식 절차로 재생성하고
--    db.verify --env dev를 실행한다. 이 파일을 직접 편집해 검증을 통과시키지 않는다.
-- 4. 개발 API에서 위기·재확인 양쪽 메시지 종류, 최근 대화 유지, 모든 파생 제외를 확인한다.
--
-- 복구: 기능을 끄고 새 파생 게시/앵커 전진을 멈춘다. 확장 CHECK와 추가 컬럼은 유지한다.
-- 기존 safety/safety_check를 삭제하거나 normal로 바꿔 구형 CHECK에 맞추지 않는다.
-- 새 종류를 읽을 수 있는 호환 코드, 현재 삭제 세대의 검증본, 허용 원문을 유지한다.
-- 읽기 호환성을 복구하기 전 구형 reader로만 되돌리지 않는다.
-- 컬럼 제거/종류 축소는 해당 데이터·작성자가 없다는 독립 검증과 별도 승인 없이는 실행하지 않는다.
