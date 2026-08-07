-- 적용: python db/apply.py db/migrations/20260805_privacy_active_backfill.sql --commit
-- 기억 재설계 2단계-(c) (docs/ARCHITECTURE-capi.md 11.3절) — active 장벽 행 backfill.
--
-- 🚨 **적용 순서 경고 — 이 파일만은 코드 배포보다 뒤다.**
--    구 코드는 `privacy_subject_barriers`에 행이 있으면 무조건 차단으로 읽는다. status-aware
--    `authorize_job`/`ensure_subject_active`가 **배포되기 전에** 이 파일을 적용하면 전 사용자의
--    대화와 잡이 즉시 막힌다.
--
--    올바른 순서:
--      1. 20260805_privacy_epoch.sql 적용            (컬럼만, 행 없음 — 안전)
--      2. status-aware 코드 배포 + 헬스 확인          ← 여기까지 끝난 뒤에
--      3. 이 파일 적용                                 (active 행 생성)
--      4. count 검증 두 sweep 통과 → privacy_barrier_mode=enforced
--
--    (다른 마이그레이션은 [[migration-before-merge]]대로 머지보다 먼저 적용한다. 이건 예외다.)
BEGIN;

-- 기존 profile 전량에 active 행. 이미 deleting/deleted인 사용자는 건드리지 않는다.
INSERT INTO public.privacy_subject_barriers (user_id, state, epoch, operation_id, high_watermark)
SELECT p.id, 'active', 0, NULL, NULL
FROM public.profiles p
ON CONFLICT (user_id) DO NOTHING;

-- 신규 가입자는 profile 생성과 **같은 transaction**에서 장벽 행을 만든다.
-- 코드 배포와 무관하게 동작해야 하므로 트리거로 둔다(가입 트리거와 같은 이유).
CREATE OR REPLACE FUNCTION public.create_privacy_barrier_for_profile()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  INSERT INTO public.privacy_subject_barriers (user_id, state, epoch)
  VALUES (NEW.id, 'active', 0)
  ON CONFLICT (user_id) DO NOTHING;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS profiles_create_privacy_barrier ON public.profiles;
CREATE TRIGGER profiles_create_privacy_barrier
  AFTER INSERT ON public.profiles
  FOR EACH ROW EXECUTE FUNCTION public.create_privacy_barrier_for_profile();

COMMIT;
