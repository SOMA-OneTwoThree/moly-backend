-- 건초팩(hay_pack)에 Google Play 상품 식별자 추가 (SOMA-342)
-- App Store 단일 상품ID 가정을 제거: 스토어별 상품ID로 조회·지급 가능하게 한다.
-- additive — 컬럼은 NULL 허용(Play Console 확정 후 UPDATE로 주입), 기존 Apple 경로 무영향.
BEGIN;

-- 스토어별 상품ID. NULL 허용(미확정 스토어는 비움). UNIQUE는 NULL 다중 허용.
ALTER TABLE public.products ADD COLUMN play_store_product_id text UNIQUE;

-- cosmetic은 스토어 상품ID를 갖지 않음 — play_store_product_id도 NULL 강제.
ALTER TABLE public.products DROP CONSTRAINT products_cosmetic_ck;
ALTER TABLE public.products ADD CONSTRAINT products_cosmetic_ck CHECK (
  product_type <> 'cosmetic' OR (
    public_id IS NOT NULL AND slot IS NOT NULL
    AND hay_amount IS NULL AND app_store_product_id IS NULL AND price_krw IS NULL
    AND play_store_product_id IS NULL
    AND is_subscriber_only = false
    AND (
      is_active = false
      OR (asset_version IS NOT NULL AND asset_version >= 1 AND assets IS NOT NULL)
    )
  )
);

-- Google Play 상품ID는 Play Console 확정 후 아래 형태로 주입한다(무배포):
--   UPDATE public.products SET play_store_product_id = 'moly_hay_300'
--     WHERE app_store_product_id = 'com.geniusjun.moly.hay.300';
COMMIT;
