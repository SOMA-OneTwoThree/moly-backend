-- 적용: python db/apply.py db/migrations/20260804_conversation_checkpoints.sql --commit
-- 대화 요약 checkpoint(W11, docs/agentic-chat-IMPLEMENTATION.md §W11) — 대화가 길어지면 오래된
-- 구간을 요약해 남기고, 다음 턴은 **최신 checkpoint 하나 + 그 이후 메시지**만 쓴다. 지금은 앵커
-- 리셋이 옛 구간을 통째로 버려서 캐피가 앞 얘기를 잊는다.
--
-- 핵심 불변식:
--  · `source_hash`는 (이전 checkpoint의 id·source_hash) + 이번 원본 메시지의 정렬된
--    (id, sender, kind, placeholder content)를 **길이-prefix**로 직렬화한 SHA-256이다(app/services/
--    checkpoint.py). 길이 prefix가 없으면 필드 경계가 모호해져 다른 입력이 같은 해시를 낼 수 있다.
--  · UNIQUE(user_id, through_message_id, source_hash) = 같은 입력을 두 번 요약해도 행이 하나다.
--    잡 dedup key(`user:…:through:…:source:…:summarizer:…`)와 짝을 이뤄 중복 LLM 호출도 막는다.
--  · through_message_id는 ON DELETE RESTRICT — 요약이 가리키는 경계 메시지는 사라질 수 없다
--    (사라지면 "이 요약이 어디까지를 덮는가"를 되짚을 수 없다).
--  · summary는 저장 표면이라 실명이 없다(`naming.to_placeholder` 통과분, {유저이름} placeholder).
--    대화 요약은 PII라 기억 테이블과 같은 등급으로 RLS + 클라 롤 권한 회수를 건다.
--
-- additive only(구버전이 읽는 동안 파괴적 변경 없음). 실제 컨텍스트 조립 배선과 킬스위치
-- (`context_checkpoint_enabled`, 기본 off)는 코드 쪽 소관이라 이 마이그레이션은 동작을 바꾸지 않는다.
BEGIN;

CREATE TABLE IF NOT EXISTS public.conversation_checkpoints (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  -- 이 요약이 덮는 마지막 메시지(경계). 다음 턴은 이 id 뒤의 메시지만 원문으로 붙인다.
  through_message_id bigint NOT NULL REFERENCES public.messages(id) ON DELETE RESTRICT,
  summary text NOT NULL,
  version text NOT NULL,        -- 요약기 계약 버전(프롬프트·출력 규약이 바뀌면 올린다)
  source_hash text NOT NULL,    -- 결정적 입력 지문(이전 checkpoint + 이번 원본 메시지)
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, through_message_id, source_hash)
);

-- "그 유저의 가장 앞선 checkpoint 하나"가 매 턴 조회다(§W11-5).
CREATE INDEX IF NOT EXISTS conversation_checkpoints_latest_idx
  ON public.conversation_checkpoints(user_id, through_message_id DESC);

-- RLS deny-default + 클라 롤 권한 회수(기억 테이블과 동일 등급 — 대화 요약은 PII).
-- 서버는 테이블 owner 롤이라 우회한다.
ALTER TABLE public.conversation_checkpoints ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.conversation_checkpoints FROM anon, authenticated;

COMMIT;
