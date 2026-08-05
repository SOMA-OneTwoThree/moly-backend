-- 적용: python db/apply.py db/migrations/20260805_push_personalization.sql --commit
-- 저녁 푸시 개인화 — 전날 대화 기반 한 줄 문구를 로컬 05시 틱에서 사전 생성해 두고,
-- 전날 첫 대화 시각(15분 격자) 슬롯에 발송한다. 유저당 1행(사이클마다 덮어씀).
--
-- 불변식:
-- - body는 naming placeholder 상태로만 저장(실명 저장 금지 — 발송 직전 render).
-- - 재사용 한도(D+3)의 정본은 anchor_date 날짜 산술. sent_count는 통계 전용(판정 사용 금지).
-- - send_slot ∈ [08:00, 20:00] 15분 격자. 20:00 = 야간(20:00~익일 08:00 첫 대화) 코호트 —
--   이들은 기존 저녁 푸시와 같은 20시 분기에서 인라인 처리된다(실패 시 같은 틱 디폴트 폴백).
BEGIN;

CREATE TABLE IF NOT EXISTS public.push_personalizations (
  user_id     uuid PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  anchor_date date NOT NULL,
  send_slot   time NOT NULL CHECK (send_slot BETWEEN TIME '08:00' AND TIME '20:00'),
  body        text NOT NULL,
  language    text NOT NULL,
  source_kind text NOT NULL CHECK (source_kind IN ('diary','transcript')),
  generated_at timestamptz NOT NULL DEFAULT now(),
  sent_count  int NOT NULL DEFAULT 0,
  last_sent_on date
);

-- body = 유저 대화에서 뽑은 PII의 파생 → RLS에 더해 클라 롤 권한도 회수한다
-- (memory_*·conversation_checkpoints와 동일 등급. diary_gen_claims의 RLS-only 등급이 아님).
ALTER TABLE public.push_personalizations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.push_personalizations FROM anon, authenticated;

COMMIT;
