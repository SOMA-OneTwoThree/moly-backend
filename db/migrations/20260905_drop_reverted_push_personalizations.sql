-- 2026-08-06 코드에서 되돌린 푸시 개인화 기능의 저장 테이블도 제거 상태로 고정한다.
-- 역사 원본 파일은 원장 재현을 위해 보존하되 현재 런타임은 이 테이블을 사용하지 않는다.
BEGIN;

DROP TABLE IF EXISTS public.push_personalizations;

DO $$
BEGIN
  IF to_regclass('public.push_personalizations') IS NOT NULL THEN
    RAISE EXCEPTION 'push_personalizations still exists after drop';
  END IF;
END $$;

COMMIT;
