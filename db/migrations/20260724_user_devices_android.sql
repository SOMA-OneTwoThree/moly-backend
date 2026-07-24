-- Android FCM 푸시 지원 — user_devices.platform 허용값에 android 추가 (SOMA-340)
-- iOS 전용 CHECK를 확장한다. 허용값 확대(additive)라 기존 iOS 행/등록 경로 무영향.
-- ⚠️ moly-auth 푸시토큰 등록 API(android 허용)와 동시 배포. 이 마이그레이션을 먼저 적용해야
--    android insert가 CHECK 위반 없이 통과한다(순서 어긋나면 android 등록 실패).
BEGIN;

ALTER TABLE public.user_devices DROP CONSTRAINT user_devices_platform_check;
ALTER TABLE public.user_devices
  ADD CONSTRAINT user_devices_platform_check CHECK (platform IN ('ios', 'android'));

COMMIT;
