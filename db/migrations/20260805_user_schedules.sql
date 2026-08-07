-- 적용: python db/apply.py db/migrations/20260805_user_schedules.sql --commit
-- 기억 재설계 4단계 (docs/ARCHITECTURE-capi.md 9.4절) — indexed due scheduler 테이블.
--
-- 지금 워커는 매 틱 전 profile을 훑어 각자의 로컬 시각을 계산한다. 사용자가 늘면 이 비용이
-- 선형으로 늘고, 15분 케이던스 안에 다 못 돌면 그날 작업이 밀린다. `next_due_at` 인덱스로
-- **due한 행만** 집는 구조로 바꾸기 위한 테이블이다.
--
-- ⚠️ 이 마이그레이션은 **읽기 경로를 바꾸지 않는다.** 테이블을 만들고 backfill만 한다.
--    문서 요구(15장 4번): schedule 4종 count가 각각 활성 profile 수 N이고 중복 0인 것을
--    확인하기 전에는 tick의 full-profile scan을 제거하거나 scheduler read로 전환하지 않는다.
BEGIN;

CREATE TABLE IF NOT EXISTS public.user_schedules (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id            uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  -- 4종 고정. 값이 계약이라 코드와 갈라지면 안 된다.
  kind               text NOT NULL CHECK (kind IN (
                       'daily_digest', 'diary_generate',
                       'diary_morning_notification', 'evening_checkin')),
  -- 발화 시점에 쓰인 timezone을 **스냅샷**으로 남긴다. 사용자가 여행 중 tz를 바꿔도
  -- 이미 계산된 due가 흔들리지 않고, revision으로만 재계산된다.
  timezone_snapshot  text NOT NULL,
  next_due_at        timestamptz NOT NULL,
  -- timezone 변경이 revision을 올리고, 진행 중 실행은 자기 revision으로 낙관적 검증한다.
  revision           bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
  last_run_at        timestamptz NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  -- 사용자×종류당 정확히 하나. 중복 0 게이트가 이 제약에 기댄다.
  CONSTRAINT user_schedules_user_kind_uniq UNIQUE (user_id, kind)
);

-- due dispatcher의 유일한 접근 경로. kind를 앞에 두어 종류별 스캔도 같은 인덱스를 쓴다.
CREATE INDEX IF NOT EXISTS user_schedules_due_idx
  ON public.user_schedules (kind, next_due_at);

COMMIT;
