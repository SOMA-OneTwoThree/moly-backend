-- 적용: python db/apply.py db/migrations/20260804_relationship_profiles.sql --commit
-- 관계 프로필(W9, docs/agentic-chat-IMPLEMENTATION.md §W9) — 정규화 기억(W8)에서 파생한
-- **칸이 고정된** 프롬프트 투영. 자유문 한 덩어리가 아니라 stance / known_facts(≤5) /
-- recent_threads(≤3) / inferred_tendencies(≤2) 네 칸이며, 렌더 결과는 ≤400토큰으로 안정
-- 프리픽스에 들어간다(하루 몇 번만 바뀌어야 캐시가 산다). 렌더러·publish 계약은
-- app/services/relationship_profile.py · relationship_profile_repo.py.
--
-- 핵심 불변식:
--  · **locale당 published는 정확히 1개** — 부분 유니크 인덱스가 DB에서 강제한다. 애플리케이션
--    검사만 두면 동시 publish에서 두 행이 함께 살아남는다(그러면 프롬프트가 유저마다 갈린다).
--  · status는 draft|published|invalidated|superseded이고 invalidated/superseded는 **terminal**이다.
--    되돌리는 UPDATE 경로가 코드에 하나도 없어야 한다(published→draft 복귀도 없다).
--  · profile source의 FK는 **복합키**(user_id 동반)다 — 단일 컬럼 FK면 A 유저의 프로필이 B 유저의
--    사실을 근거로 삼는 경로가 열린다. W8의 superseded_by·memory_insight_sources와 같은 방식.
--  · 근거는 fact 또는 insight 중 정확히 하나(num_nonnulls=1)이고, 같은 item_key 안에서 같은 근거를
--    두 번 달 수 없다(부분 유니크 2종).
--  · ON DELETE RESTRICT — 근거 없는 프로필만 남는 상태를 스키마가 막는다(삭제는 W10 retention 절차).
--
-- additive only(기존 테이블·컬럼을 바꾸지 않는다). W8(20260804_memory_normalization.sql)이
-- 이 두 테이블을 일부러 남겨둔 자리이므로 그 마이그레이션 **다음에** 적용한다.
BEGIN;

-- ─────────────────────────────────────────────────────────────
-- 프로필 문서 — locale별 버전 이력. version은 render_hash가 바뀔 때만 올라간다
-- (같은 내용 재발행 금지 = 안정 프리픽스 캐시 유지). memory_generation·
-- relationship_profile_input_revision은 draft를 만든 시점의 chat_contexts 값이고,
-- publish 트랜잭션이 잠근 현재값과 다르면 결과를 폐기한다(stale_generation/stale_profile_input).
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.relationship_profiles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  version bigint NOT NULL CHECK (version > 0),
  locale text NOT NULL,                     -- i18n 버킷(ko|en|ja). 언어를 바꾸면 새 locale 행이 생긴다
  memory_generation bigint NOT NULL,
  relationship_profile_input_revision bigint NOT NULL,
  document_json jsonb NOT NULL,             -- 칸별 항목 + item_key + source_refs(edge와 양방향 일치)
  rendered_text text NOT NULL,              -- 프롬프트에 실리는 문자열(실명 금지 — {유저이름} placeholder)
  render_hash text NOT NULL,                -- 같은 값이면 새 version을 만들지 않는다
  status text NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft','published','invalidated','superseded')),
  created_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz NULL,
  UNIQUE (user_id, id),                     -- 아래 복합 FK가 user_id를 함께 태우기 위한 대상 키
  UNIQUE (user_id, locale, version),
  CHECK ((status='published' AND published_at IS NOT NULL) OR status<>'published')
);
-- locale당 published 정확히 1개 — 동시성까지 DB가 강제한다.
CREATE UNIQUE INDEX IF NOT EXISTS relationship_profiles_one_published_idx
  ON public.relationship_profiles(user_id, locale) WHERE status='published';

-- ─────────────────────────────────────────────────────────────
-- 근거 간선 — document_json의 source_refs와 **양방향으로 정확히 같아야** publish한다
-- (type/id/item_key까지). 렌더 시점에도 각 근거가 지금 active인지·forget marker에 걸리지
-- 않는지 다시 대조한다 — 재생성이 밀려도 잊은 내용이 프롬프트로 새지 않게.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.relationship_profile_sources (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL,
  relationship_profile_id uuid NOT NULL,
  item_key text NOT NULL,                   -- 문서 항목의 불변 키(칸 안에서 항목을 식별)
  fact_id uuid NULL,
  insight_id uuid NULL,
  CHECK (num_nonnulls(fact_id, insight_id)=1),
  FOREIGN KEY (user_id, relationship_profile_id)
    REFERENCES public.relationship_profiles(user_id, id) ON DELETE CASCADE,
  -- 복합 FK — 타 유저의 fact/insight를 근거로 다는 경로를 스키마가 막는다.
  FOREIGN KEY (user_id, fact_id) REFERENCES public.memory_facts(user_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (user_id, insight_id) REFERENCES public.memory_insights(user_id, id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS relationship_profile_sources_fact_uq
  ON public.relationship_profile_sources(user_id, relationship_profile_id, item_key, fact_id)
  WHERE fact_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS relationship_profile_sources_insight_uq
  ON public.relationship_profile_sources(user_id, relationship_profile_id, item_key, insight_id)
  WHERE insight_id IS NOT NULL;
-- RESTRICT 검사(fact/insight 삭제 시 참조 탐색)와 근거 역추적용. 위 유니크 인덱스는
-- (user_id, profile_id, item_key)로 시작해 근거 id 단독 조회를 못 받는다.
CREATE INDEX IF NOT EXISTS relationship_profile_sources_fact_idx
  ON public.relationship_profile_sources(user_id, fact_id) WHERE fact_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS relationship_profile_sources_insight_idx
  ON public.relationship_profile_sources(user_id, insight_id) WHERE insight_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────────
-- RLS deny-default + 클라 롤 권한 회수(기억 테이블과 같은 불변식).
-- 프로필은 유저 PII(대화에서 뽑은 사실)에서 파생되므로 직접 접근 경로를 두지 않는다.
-- ─────────────────────────────────────────────────────────────
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['relationship_profiles','relationship_profile_sources'] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY;', t);
    EXECUTE format('REVOKE ALL ON public.%I FROM anon, authenticated;', t);
  END LOOP;
END $$;

COMMIT;
