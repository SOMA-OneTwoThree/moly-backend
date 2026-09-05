-- 기존 건초 광고 세션에 짧은 수명을 부여해 무제한 선발급·장기 비축을 막는다.
-- 기존 미지급 세션은 생성 후 30분으로 backfill되며 이미 만료됐으면 지급되지 않는다.
BEGIN;

-- 테이블은 운영 실측 802행(2026-09-05)으로 작지만, 잠금 대기로 요청을 줄 세우지 않도록
-- 즉시 실패 후 재시도하는 운영 규칙을 동일하게 적용한다.
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE public.reward_ad_sessions
  ADD COLUMN IF NOT EXISTS expires_at timestamptz;

UPDATE public.reward_ad_sessions
   SET expires_at = created_at + interval '30 minutes'
 WHERE expires_at IS NULL;

ALTER TABLE public.reward_ad_sessions
  ALTER COLUMN expires_at SET DEFAULT (now() + interval '30 minutes'),
  ALTER COLUMN expires_at SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid='public.reward_ad_sessions'::regclass
      AND conname='reward_ad_sessions_expiry_ck'
  ) THEN
    ALTER TABLE public.reward_ad_sessions
      ADD CONSTRAINT reward_ad_sessions_expiry_ck CHECK (expires_at > created_at);
  END IF;
END $$;

DROP INDEX IF EXISTS public.reward_ad_sessions_pending_expiry_idx;
CREATE INDEX IF NOT EXISTS reward_ad_sessions_expiry_idx
  ON public.reward_ad_sessions (expires_at, session_id);

COMMIT;
