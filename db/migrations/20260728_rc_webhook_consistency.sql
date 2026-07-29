-- 적용: python db/apply.py db/migrations/20260728_rc_webhook_consistency.sql --commit
-- SOMA-372 RC 웹훅 정합성 — 내구 inbox + 상태 단조 + 환불 회수(음수 부채).
--  1) revenuecat_events inbox(웹훅 raw 영속·재시도·관측)
--  2) subscriptions.last_event_at(상태 단조 기준)
--  3) profiles.hay_balance>=0 CHECK 완화(음수=부채 → 완전 회수)
--  4) 기존 active·grace_period 구독 last_event_at backfill(배포 직후 옛 이벤트 무시).
-- ⚠️ 배포 = no-mixed-webhook(구버전 pod는 inbox 미저장) — 웹훅 유입 일시중단 또는 신버전 전용
--    라우팅으로 배포한다(배포 런북). 워커 케이던스 15분(SOMA-348) 전제.
BEGIN;

-- 1. RC 웹훅 내구 inbox — 엔드포인트가 raw를 커밋 후 process_event로 소비. 실패/미결은 워커 관측.
CREATE TABLE IF NOT EXISTS public.revenuecat_events (
  event_id     text        PRIMARY KEY,               -- RC event.id(전역 유일) — 중복 웹훅 멱등
  payload      jsonb       NOT NULL,                  -- 이벤트 원문(운영 수동 처리·재처리 근거)
  status       text        NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','processed','failed')),
  attempts     integer     NOT NULL DEFAULT 0,        -- 예상 밖 예외 재시도 횟수(≥5 → failed)
  received_at  timestamptz NOT NULL DEFAULT now(),
  next_attempt_at timestamptz NOT NULL DEFAULT now(), -- 다음 재시도 예약(backoff) — 드레인 후보·정렬 기준(rotation)
  processed_at timestamptz,                           -- processed/failed 확정 시각
  last_error   text                                   -- 실패 사유 또는 no-op durable reason
);
-- 이미 생성된 환경(구버전 CREATE TABLE)엔 컬럼이 없으므로 멱등 ADD(있으면 no-op).
ALTER TABLE public.revenuecat_events
  ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz NOT NULL DEFAULT now();
-- 기존 pending 행 backfill — 재시도 예약을 received_at으로 정렬해 도착순 rotation 시작점 확보.
UPDATE public.revenuecat_events SET next_attempt_at = received_at WHERE status = 'pending';
-- 워커 재요약 인덱스(미해결 failed·장기 pending 스캔 — received_at 정렬).
CREATE INDEX IF NOT EXISTS revenuecat_events_status_idx
  ON public.revenuecat_events (status, received_at);
-- 드레인 후보 인덱스 — pending 중 next_attempt_at <= now() 스캔·정렬(집합 내부 rotation).
CREATE INDEX IF NOT EXISTS revenuecat_events_status_next_attempt_idx
  ON public.revenuecat_events (status, next_attempt_at);

-- RLS deny-default(다른 테이블과 동일 불변식) — 서버는 owner 롤이라 우회, 클라 직접 접근 차단.
ALTER TABLE public.revenuecat_events ENABLE ROW LEVEL SECURITY;

-- 2. 상태 단조 기준 — 이 시각 이하의 옛 상태 이벤트는 활성 구독을 되돌리지 않는다.
ALTER TABLE public.subscriptions ADD COLUMN IF NOT EXISTS last_event_at timestamptz;

-- 3. 부채 허용 — 증정 소비 후 환불 시 잔액이 음수(빚)로 내려가 이후 획득이 자연 상계(완전 회수).
--    소비 경로(<0 게이트)는 코드가 계속 막는다(hay_ledger.apply allow_negative=False 기본).
-- ⚠️ 배포 전 실제 제약명 확인(인라인 CHECK 자동명 규칙 = <table>_<column>_check):
--    SELECT conname FROM pg_constraint WHERE conrelid='public.profiles'::regclass AND contype='c';
--    이름이 다르면 아래 DROP은 no-op이 되어 회수(음수 INSERT)가 실패하므로 실제 이름으로 교체한다.
ALTER TABLE public.profiles DROP CONSTRAINT IF EXISTS profiles_hay_balance_check;

-- 4. backfill — 기존 active·grace_period(혜택 유지) 구독에 배포시각 sentinel. last_event_at NULL이면
--    "첫 이벤트는 항상 적용"이라 배포 직후 도착한 과거 이벤트 1건이 활성 구독을 되돌릴 수 있다.
UPDATE public.subscriptions
   SET last_event_at = now()
 WHERE status IN ('active', 'grace_period') AND last_event_at IS NULL;

COMMIT;
