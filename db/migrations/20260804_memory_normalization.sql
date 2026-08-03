-- 적용: python db/apply.py db/migrations/20260804_memory_normalization.sql --commit
-- 메모리 정규화(W8, docs/agentic-chat-IMPLEMENTATION.md §W8) — 자유문 기억(mem0)을 대체할
-- **턴 단위 구조화 사실** 저장소. 판정(ADD/REINFORCE/SUPERSEDE/KEEP_BOTH/IGNORE)은 LLM이 아니라
-- 코드가 한다(app/services/memory_reconcile.py). mem0는 폐기가 아니라 병행이며 유저별 전환은
-- chat_contexts.memory_mode('legacy'|'normalized')가 가른다. 실제 cutover는 W10 소관.
--
-- 핵심 불변식:
--  · fact의 (normalization_version, content_hash)와 marker의 (normalization_version, normalized_hash)는
--    **같은 산출물**이다. forget 시 fact의 hash/version을 marker로 그대로 복사한다. 검색·hard filter는
--    f.normalization_version=m.normalization_version AND f.content_hash=m.normalized_hash로 비교한다.
--  · fact의 forgotten/superseded, insight의 invalidated/superseded는 **terminal**이다. 같은 내용을
--    다시 관찰해도 기존 행을 active로 되살리지 않는다(되살리는 UPDATE 경로가 코드에 없어야 한다).
--  · watermark는 대화 turn당 정확히 하나(memory_source_turns). 한 message는 정확히 한 watermark에만
--    속한다(memory_source_turn_messages의 UNIQUE(user_id, message_id)).
--  · v1 추출 소스는 conversation_turn만이다. 일기·루틴·프로필 이벤트는 각자의 watermark/closure
--    계약이 생기기 전까지 CHECK enum에 추가하지 않는다.
--  · evidence/insight source/profile source의 FK는 ON DELETE RESTRICT다 — 근거 없는 파생만 남는 상태를
--    스키마가 막는다. 삭제는 W10 retention 절차(파생 → 근거 → 마지막에 marker)로만 한다.
--
-- additive expand만(구버전이 읽는 동안 파괴적 변경 없음). relationship_profiles·
-- relationship_profile_sources는 W9 소관이라 여기서 만들지 않는다.
BEGIN;

-- fact 임베딩(memory_facts.embedding)용. dev는 v0.8.2 설치 확인됨. 차원 고정은 embedder
-- 마이그레이션에서 별도 검증한다(여기서는 무차원 vector).
CREATE EXTENSION IF NOT EXISTS vector;

-- ─────────────────────────────────────────────────────────────
-- 사실(fact) + 근거(evidence)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.memory_facts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  kind text NOT NULL,                       -- profile|preference|relationship|event|emotion (코드 registry)
  canonical_text text NOT NULL,             -- 자연어 표면 — 저장 직전 naming.to_placeholder + 살균 강제
  subject text NULL,
  predicate text NULL,                      -- 코드 registry의 canonical key(cardinality single|multi)
  object_json jsonb NULL,                   -- predicate와 함께 있거나 함께 없다(스키마 검증)
  event_time timestamptz NULL,
  valid_from timestamptz NOT NULL DEFAULT now(),
  valid_to timestamptz NULL,
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','superseded','forgotten')),
  importance double precision NOT NULL,
  confidence double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  content_hash text NOT NULL,               -- = marker.normalized_hash와 같은 산출물(공용 해시 함수)
  normalization_version text NOT NULL,      -- 예: memory-fact-v1. 제자리 재해시 금지(구 normalizer 영구 보관)
  superseded_by uuid NULL,
  embedding vector NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, id),                     -- 아래 복합 FK들이 user_id를 함께 태우기 위한 대상 키
  FOREIGN KEY (user_id, superseded_by) REFERENCES public.memory_facts(user_id, id) ON DELETE RESTRICT,
  CHECK ((status='active' AND valid_to IS NULL) OR status<>'active')
);
CREATE INDEX IF NOT EXISTS memory_facts_active_user_idx
  ON public.memory_facts(user_id, predicate, event_time) WHERE status='active';
CREATE INDEX IF NOT EXISTS memory_facts_hash_idx
  ON public.memory_facts(user_id, normalization_version, content_hash);

-- 근거. FK가 user_id를 태우지 않으므로(messages는 (id) PK) **코드가 트랜잭션 안에서
-- messages.user_id = fact.user_id를 반드시 검증한다** — DB 제약만으로는 타 유저 메시지를 못 막는다.
CREATE TABLE IF NOT EXISTS public.memory_evidence (
  fact_id uuid NOT NULL REFERENCES public.memory_facts(id) ON DELETE RESTRICT,
  source_type text NOT NULL CHECK (source_type='conversation_turn'),  -- v1은 이 값만
  source_id bigint NOT NULL REFERENCES public.messages(id) ON DELETE RESTRICT,
  source_excerpt_hash text NOT NULL,
  observed_at timestamptz NOT NULL,
  PRIMARY KEY (fact_id, source_type, source_id)
);

-- ─────────────────────────────────────────────────────────────
-- 통찰(insight) — fact에서 파생. active→invalidated|superseded만 허용(terminal 불가역).
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.memory_insights (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  text text NOT NULL,
  confidence double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','invalidated','superseded')),
  valid_from timestamptz NOT NULL DEFAULT now(),
  valid_to timestamptz NULL,
  derivation_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, id),
  CHECK ((status='active' AND valid_to IS NULL) OR status<>'active')
);

CREATE TABLE IF NOT EXISTS public.memory_insight_sources (
  user_id uuid NOT NULL,
  insight_id uuid NOT NULL,
  fact_id uuid NOT NULL,
  PRIMARY KEY (user_id, insight_id, fact_id),
  -- 복합 FK(user_id 동반) — 타 유저 fact를 근거로 다는 경로를 스키마가 막는다.
  FOREIGN KEY (user_id, insight_id) REFERENCES public.memory_insights(user_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (user_id, fact_id) REFERENCES public.memory_facts(user_id, id) ON DELETE RESTRICT
);

-- ─────────────────────────────────────────────────────────────
-- 망각 마커 — "잊어달라"의 영속 deny key. 검색·추출 hard filter가 모든 LLM 제안보다 먼저 본다.
-- fact scope는 그 행의 content_hash/normalization_version을 그대로 복사해 둔다(hash 이중 신원 금지).
-- 사용자 marker의 expires_at은 NULL이다(CHECK로 못 박음) — 잊은 사실이 만료로 되살아나지 않는다.
-- fact_id FK는 DEFERRABLE INITIALLY DEFERRED — retention 삭제 시 marker를 **마지막 statement**로
-- 지울 수 있게 하기 위함이다(중간 실패는 전부 rollback).
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.memory_forget_markers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  scope text NOT NULL CHECK (scope IN ('fact','predicate','all')),
  fact_id uuid NULL,
  normalized_hash text NULL,                -- = memory_facts.content_hash 복사본
  normalization_version text NULL,          -- = memory_facts.normalization_version 복사본
  predicate text NULL,
  memory_generation bigint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NULL,
  FOREIGN KEY (user_id, fact_id) REFERENCES public.memory_facts(user_id, id)
    ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
  CHECK (
    (scope='fact' AND fact_id IS NOT NULL AND normalized_hash IS NOT NULL
                  AND normalization_version IS NOT NULL AND predicate IS NULL)
    OR (scope='predicate' AND fact_id IS NULL AND normalized_hash IS NULL
                          AND normalization_version IS NULL AND predicate IS NOT NULL)
    OR (scope='all' AND fact_id IS NULL AND normalized_hash IS NULL
                    AND normalization_version IS NULL AND predicate IS NULL)
  ),
  CHECK (expires_at IS NULL)
);
CREATE INDEX IF NOT EXISTS memory_forget_markers_match_idx
  ON public.memory_forget_markers(user_id, scope, normalization_version, normalized_hash, predicate);

-- ─────────────────────────────────────────────────────────────
-- 소스 watermark — 대화 turn당 정확히 하나. representative_message_id는 그 turn을 시작한
-- **inbound user message**다. 이 메시지가 없는 turn(선발화만 등)은 추출 소스로 enqueue하지 않는다.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.memory_source_turns (
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  source_watermark bigint NOT NULL CHECK (source_watermark > 0),
  representative_message_id bigint NOT NULL REFERENCES public.messages(id) ON DELETE RESTRICT,
  committed_at timestamptz NOT NULL,
  PRIMARY KEY (user_id, source_watermark),
  UNIQUE (user_id, representative_message_id)
);

-- 같은 turn에서 evidence로 허용할 user/assistant 메시지 전부(대표 포함). 한 message는 정확히
-- 한 watermark에만 속한다 → evidence.source_id의 watermark를 이 표로 되찾는다.
CREATE TABLE IF NOT EXISTS public.memory_source_turn_messages (
  user_id uuid NOT NULL,
  source_watermark bigint NOT NULL,
  message_id bigint NOT NULL REFERENCES public.messages(id) ON DELETE RESTRICT,
  PRIMARY KEY (user_id, source_watermark, message_id),
  UNIQUE (user_id, message_id),
  FOREIGN KEY (user_id, source_watermark)
    REFERENCES public.memory_source_turns(user_id, source_watermark) ON DELETE RESTRICT
);

-- forget이 닫은 소스 구간. 추출 배치의 중간 watermark가 하나라도 겹치면 **부분 publish 금지** —
-- 전체를 source_range_closed로 끝내고 열린 source만 새 generation job으로 다시 묶는다.
CREATE TABLE IF NOT EXISTS public.memory_source_closures (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  source_kind text NOT NULL CHECK (source_kind='conversation_turn'),
  from_watermark bigint NOT NULL,
  through_watermark bigint NOT NULL,
  forget_operation_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (from_watermark <= through_watermark),
  UNIQUE (user_id, forget_operation_id, source_kind, from_watermark, through_watermark)
);
CREATE INDEX IF NOT EXISTS memory_source_closures_overlap_idx
  ON public.memory_source_closures(user_id, source_kind, from_watermark, through_watermark);

-- ─────────────────────────────────────────────────────────────
-- chat_contexts 확장 — 유저별 모드/세대/워터마크/프로필 입력 리비전.
-- memory_mode 기본값은 'legacy'다. W8은 스키마·저장소만 놓고 유저를 옮기지 않는다(cutover=W10).
-- relationship_profile_input_revision은 fact/evidence/insight의 **실제** 내용·source·상태 변경
-- 트랜잭션에서만 정확히 1 증가한다(no-op/retry/임베딩 재색인은 증가시키지 않는다).
-- ─────────────────────────────────────────────────────────────
ALTER TABLE public.chat_contexts
  ADD COLUMN IF NOT EXISTS memory_mode text NOT NULL DEFAULT 'legacy',
  ADD COLUMN IF NOT EXISTS memory_generation bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS memory_source_watermark bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS relationship_profile_input_revision bigint NOT NULL DEFAULT 0;

DO $$
BEGIN
  IF NOT EXISTS (
    -- conrelid까지 봐야 한다. 이름만 보면 다른 테이블에 동명 제약이 있을 때
    -- CHECK 추가가 조용히 생략된다(제약 없는 채로 배포됨).
    SELECT 1 FROM pg_constraint
    WHERE conname='chat_contexts_memory_mode_check'
      AND conrelid='public.chat_contexts'::regclass
  ) THEN
    ALTER TABLE public.chat_contexts
      ADD CONSTRAINT chat_contexts_memory_mode_check CHECK (memory_mode IN ('legacy','normalized'));
  END IF;
END $$;

-- ─────────────────────────────────────────────────────────────
-- RLS deny-default + 클라 롤 권한 회수(다른 민감 테이블과 같은 불변식).
-- 서버는 테이블 owner 롤이라 우회한다. 기억은 유저 대화에서 뽑은 PII라 직접 접근 경로를 두지 않는다.
-- ─────────────────────────────────────────────────────────────
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'memory_facts','memory_evidence','memory_insights','memory_insight_sources',
    'memory_forget_markers','memory_source_turns','memory_source_turn_messages',
    'memory_source_closures'
  ] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY;', t);
    EXECUTE format('REVOKE ALL ON public.%I FROM anon, authenticated;', t);
  END LOOP;
END $$;

COMMIT;
