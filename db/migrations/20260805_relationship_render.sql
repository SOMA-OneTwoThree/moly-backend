-- 적용: python db/apply.py db/migrations/20260805_relationship_render.sql --commit
-- 기억 재설계 5단계 보완 (docs/capi-memory-ARCHITECTURE.md 7.2절) — 관계 event 좌표와 locale render.
--
-- 관계 시작 시각은 `profiles`가 정본이다. state에 복제해 두 번째 정본을 만들지 않는다.
-- locale render는 같은 snapshot에서 profile과 state를 함께 읽어 만든 **projection**이다.
BEGIN;

-- event는 turn 좌표를 갖는다. dedup_key만으로는 어떤 turn인지 추적할 수 없다.
ALTER TABLE public.relationship_events
  ADD COLUMN IF NOT EXISTS turn_seq bigint NULL CHECK (turn_seq IS NULL OR turn_seq >= 0);

-- event_type을 계약으로 고정한다(자유 문자열이면 집계가 조용히 갈라진다).
ALTER TABLE public.relationship_events
  DROP CONSTRAINT IF EXISTS relationship_events_type_ck;
ALTER TABLE public.relationship_events
  ADD CONSTRAINT relationship_events_type_ck
  CHECK (event_type IN ('normal_turn_committed','active_day_started'));

-- 일반 profile 수정이 프롬프트 캐시 identity를 바꾸지 않게, 관계 표시 3필드가 바뀔 때만 증가시킨다.
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS relationship_revision bigint NOT NULL DEFAULT 0
    CHECK (relationship_revision >= 0);

-- locale별 렌더 결과. 결정적 state가 바뀌지 않은 채 locale만 바뀌면 projection만 새로 만든다.
CREATE TABLE IF NOT EXISTS public.relationship_profile_renders (
  id                            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id                       uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  prompt_revision               bigint NOT NULL,
  profile_relationship_revision bigint NOT NULL,
  locale                        text NOT NULL,
  renderer_version              text NOT NULL,
  rendered_text                 text NOT NULL,
  render_hash                   text NOT NULL,
  created_at                    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, prompt_revision, profile_relationship_revision, locale, renderer_version)
);

CREATE INDEX IF NOT EXISTS relationship_profile_renders_lookup_idx
  ON public.relationship_profile_renders (user_id, locale, prompt_revision DESC);

ALTER TABLE public.relationship_profile_renders ENABLE ROW LEVEL SECURITY;

COMMIT;
