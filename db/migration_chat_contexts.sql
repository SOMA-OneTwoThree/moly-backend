-- 프롬프트 캐싱: 대화 컨텍스트 상태(앵커+기억스냅샷) + messages 캐시 텔레메트리 컬럼.
-- 아웃티지 없음: 인덱스 빌드/복합 FK 없음. ADD COLUMN(nullable, no default)은 PG11+ 메타데이터 전용.
SET lock_timeout = '3s';

CREATE TABLE IF NOT EXISTS public.chat_contexts (
  user_id             uuid PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  anchor_message_id   bigint NOT NULL DEFAULT 0 CHECK (anchor_message_id >= 0),
  memory_text         text,
  memory_refreshed_at timestamptz,
  updated_at          timestamptz NOT NULL DEFAULT now()
);
-- 민감(기억 평문 사본) → deny-default. anon/authenticated 직접 접근 차단(서버는 owner라 우회).
ALTER TABLE public.chat_contexts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.chat_contexts FROM anon, authenticated;

ALTER TABLE public.messages
  ADD COLUMN IF NOT EXISTS cache_read_tokens  integer,
  ADD COLUMN IF NOT EXISTS cache_write_tokens integer,
  ADD COLUMN IF NOT EXISTS billable_tokens    integer;
