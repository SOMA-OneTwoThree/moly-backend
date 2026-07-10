-- 광고 보상 = SSV 자동 지급 전환: 세션 테이블 도입, 옛 ad_rewards 폐기.
CREATE TABLE IF NOT EXISTS public.reward_ad_sessions (
  session_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id            uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  activity_date      date NOT NULL,
  ssv_transaction_id text UNIQUE,
  granted            boolean NOT NULL DEFAULT false,
  created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS reward_ad_sessions_user_idx ON public.reward_ad_sessions (user_id);
ALTER TABLE public.reward_ad_sessions ENABLE ROW LEVEL SECURITY;
DROP TABLE IF EXISTS public.ad_rewards CASCADE;
