-- 적용: python db/apply.py db/migrations/20260805_privacy_epoch.sql --commit
-- 기억 재설계 2단계-(a) (docs/ARCHITECTURE-capi.md 11.3절) — 삭제 장벽에 상태·epoch 추가.
--
-- ⚠️ 순서가 이 마이그레이션의 전부다.
--   현행 코드는 `privacy_subject_barriers`에 **행이 있으면 차단**으로 읽는다(state를 보지 않음).
--   그래서 여기서는 컬럼만 additive로 넣고 **행을 만들지 않는다.** 지금 전 사용자에게
--   `active` 행을 깔면 현행 "행 존재 = 차단" 경로가 전체 사용자를 막아 서비스가 죽는다.
--   순서: (a) 이 마이그레이션 → (b) status-aware authorize_job 배포 → (c) active 행 backfill
--         → (d) privacy_barrier_mode=enforced 전환.
--
-- 기존 행은 전부 deleting/deleted다. CHECK만 넓히고 값은 건드리지 않으므로 해석이 바뀌지 않는다.
BEGIN;

-- state에 'active'를 허용한다. 기존 값(deleting/deleted)의 의미는 그대로다.
ALTER TABLE public.privacy_subject_barriers
  DROP CONSTRAINT IF EXISTS privacy_subject_barriers_state_check;
ALTER TABLE public.privacy_subject_barriers
  ADD CONSTRAINT privacy_subject_barriers_state_check
  CHECK (state IN ('active','deleting','deleted'));

-- epoch — 삭제 사이클 세대. running 잡의 stage token을 무효화하는 fencing 좌표다.
-- 기존 행(이미 삭제 진행/완료)도 0에서 시작한다. 다음 삭제가 1로 올린다.
ALTER TABLE public.privacy_subject_barriers
  ADD COLUMN IF NOT EXISTS epoch bigint NOT NULL DEFAULT 0 CHECK (epoch >= 0);

-- operation_id는 active 행에는 없다(삭제 작업이 아직 없으므로). nullable로 완화한다.
ALTER TABLE public.privacy_subject_barriers
  ALTER COLUMN operation_id DROP NOT NULL;

-- active 행에는 operation_id가 없고, deleting/deleted에는 반드시 있다.
ALTER TABLE public.privacy_subject_barriers
  DROP CONSTRAINT IF EXISTS privacy_subject_barriers_operation_ck;
ALTER TABLE public.privacy_subject_barriers
  ADD CONSTRAINT privacy_subject_barriers_operation_ck
  CHECK ((state = 'active') OR (operation_id IS NOT NULL));

-- (c) 단계의 keyset backfill과 (d)의 count 검증이 매번 풀스캔하지 않게.
CREATE INDEX IF NOT EXISTS privacy_subject_barriers_state_idx
  ON public.privacy_subject_barriers (state);

COMMIT;
