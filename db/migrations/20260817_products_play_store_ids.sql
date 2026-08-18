-- 적용: python db/apply.py db/migrations/20260817_products_play_store_ids.sql --env prod --commit --allow-prod
-- SOMA-342 후속: 건초팩(hay_pack)에 Google Play 상품ID 주입 — 안드로이드 소모성 구매 지급 활성화.
--
-- 배경: 20260724_products_play_store_id.sql 이 컬럼만 추가하고 값 주입은 "Play Console 확정 후"로
--   미뤄둔 상태였다. 그 결과 안드로이드 소모성 구매의 RC 웹훅(NON_RENEWING_PURCHASE, store=PLAY_STORE)이
--   payment.grant_pack 의 play_store_product_id 조회에서 미상 상품 → permanent_failure 로 떨어져
--   건초가 지급되지 않았다(2026-08-16 실제 SANDBOX 구매 2건이 inbox failed 로 적재됨).
--
-- Play 상품ID는 App Store 와 동일하다 — 실제 수신 이벤트의 product_id 가
--   'com.geniusjun.moly.hay.300' 로 Apple 것과 같음이 확인됐다. 그래서 app_store_product_id 를 그대로 복사한다.
--   (추측이 아니라 수신 payload 근거. 향후 Play ID가 갈라지면 이 마이그레이션이 아니라 개별 UPDATE로 정정한다.)
--
-- 멱등: 이미 채워진 행은 건드리지 않는다(IS NULL 조건). 되돌리기 = play_store_product_id 를 NULL 로.
BEGIN;

UPDATE public.products
   SET play_store_product_id = app_store_product_id
 WHERE product_type = 'hay_pack'
   AND play_store_product_id IS NULL
   AND app_store_product_id IS NOT NULL;

-- 검증: 활성 건초팩 중 Play ID 미주입이 남아 있으면 실패시킨다(조용한 부분 적용 금지).
DO $$
DECLARE
  v_missing integer;
BEGIN
  SELECT count(*) INTO v_missing
  FROM public.products
  WHERE product_type = 'hay_pack' AND is_active = true AND play_store_product_id IS NULL;
  IF v_missing > 0 THEN
    RAISE EXCEPTION '활성 건초팩 % 건에 play_store_product_id 가 비어 있다', v_missing;
  END IF;
END $$;

COMMIT;
