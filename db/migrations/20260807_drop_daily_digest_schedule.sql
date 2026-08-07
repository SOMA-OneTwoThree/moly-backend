-- 적용: python db/apply.py db/migrations/20260807_drop_daily_digest_schedule.sql --commit
-- daily_digest 스케줄 슬롯 제거 — revert된 푸시 개인화(#177, 2a58f56)의 마지막 잔재.
--
-- `daily_digest`(로컬 05시)는 구 push_personalization.GEN_HOUR가 쓰려고 예약해 둔 슬롯이다.
-- 그 기능은 2026-08-07 전면 revert됐고 재개 계획이 없어, 코드(user_schedules.KINDS 3종)와
-- DB 계약을 다시 맞춘다. 20260805_user_schedules.sql은 이미 적용·기록(checksum)돼 있어
-- 수정하지 않고 새 파일로 조인다.
--
-- 읽기 경로 영향 없음: dispatcher는 schedule_dispatcher_enabled=False(기본 꺼짐)이고,
-- 켜져 있어도 daily_digest kind를 소비하는 코드가 없다.
BEGIN;

-- backfill이 이미 돈 DB(dev)에는 daily_digest 행이 있다 — 제약을 조이기 전에 지운다.
DELETE FROM public.user_schedules WHERE kind = 'daily_digest';

-- 인라인 CHECK의 자동 이름. 3종으로 재정의한다 — 값이 계약이라 코드와 갈라지면 안 된다.
ALTER TABLE public.user_schedules
  DROP CONSTRAINT IF EXISTS user_schedules_kind_check;
ALTER TABLE public.user_schedules
  ADD CONSTRAINT user_schedules_kind_check CHECK (kind IN (
    'diary_generate', 'diary_morning_notification', 'evening_checkin'));

COMMIT;
