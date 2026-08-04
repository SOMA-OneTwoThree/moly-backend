-- 적용: python db/apply.py db/migrations/20260804_chat_last_active.sql --commit
-- 직전 대화 시각. 현재 요청의 Phase 1에서 읽고 Phase 2 확정 때 갱신해 "오랜만" 판정이
-- 현재 요청 자체를 보지 않도록 한다.
BEGIN;

ALTER TABLE public.chat_contexts
  ADD COLUMN IF NOT EXISTS last_active_at timestamptz NULL;

COMMIT;
