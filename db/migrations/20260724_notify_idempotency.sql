-- 알림 발송 멱등 마커 (SOMA-348)
-- 유저×활동일당 아침·저녁 각 1회 발송을 보장 — 워커 재시도·중복 실행·15분 케이던스에서
-- 중복 푸시 방지. 기존 diary 멱등(diaries UNIQUE)과 짝을 이룬다.
-- additive — nullable 컬럼 추가, 기존 행/경로 무영향.
BEGIN;

ALTER TABLE public.user_daily_stats ADD COLUMN morning_notified_at timestamptz;
ALTER TABLE public.user_daily_stats ADD COLUMN evening_notified_at timestamptz;

COMMIT;
