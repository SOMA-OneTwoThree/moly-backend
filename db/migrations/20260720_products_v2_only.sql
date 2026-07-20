-- v2 전용 꾸미기 플래그 — 신버전(rightside 자세) 계약에만 노출하고 레거시(구버전)
-- 카탈로그/인벤토리에서는 제외한다. 레거시 응답은 detail_url을 필수로 요구하므로
-- rightside만 있는 신규 아이템(예: head_glasses)이 섞이면 검증 실패로 500이 난다.
-- 적용: python db/apply.py db/migrations/20260720_products_v2_only.sql --commit
BEGIN;

ALTER TABLE public.products
  ADD COLUMN is_v2_only boolean NOT NULL DEFAULT false;

COMMIT;
