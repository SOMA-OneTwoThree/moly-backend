-- Release preparation: 오늘의 글귀 당일 확인 표시, 기존 dev/prod DB에는 없는 컬럼이다.
-- Apply with db.apply only after inspecting the target schema and reviewing the hash.
-- Source of truth remains db/schema.sql; nullable 추가라 기존 배포와 혼합 실행해도 호환된다.
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
-- 유저 × 현지 날짜 1행에 확인 시각을 기록한다. NULL이면 미확인(배너 노출).
ALTER TABLE public.user_daily_stats ADD COLUMN IF NOT EXISTS affirmation_acknowledged_at timestamptz;
