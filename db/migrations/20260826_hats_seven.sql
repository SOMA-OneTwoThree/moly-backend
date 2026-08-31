-- 적용: python db/apply.py db/migrations/20260826_hats_seven.sql --commit
--   (키 없는 기기에서는 Supabase MCP apply_migration으로 같은 SQL을 적용한다 —
--    apply.py가 그렇듯 MCP도 자체 트랜잭션으로 감싸므로 BEGIN/COMMIT 줄은 빼고 넣는다.)
--
-- 모자(hat 슬롯) 7종 신규 등록: 버킷햇·캡모자·빵모자·두건·수건·수박 모자·토끼 모자.
--
-- 선행 조건
--   1) 20260719_hat_glasses_rightside.sql 적용 완료 — slot 'hat'이 있어야 한다.
--   2) shop-assets 버킷에 7종의 {public_id}/v1/thumb.png(200×200)와
--      {public_id}/v1/rightside/upright.png(800×1100)가 올라가 있어야 한다.
--      착용 레이어는 iOS 번들 캐릭터(cappy.imageset, 800×1100)와 픽셀 정렬이 같아야 한다.
--
-- ⚠️ 7종 모두 신규 자세(rightside) 레이어만 있고 detail_url·구 자세 upright_layer_url이 없다.
--    head_glasses(20260720_products_v2_only.sql)와 같은 v2 전용 상품이므로 is_v2_only=true로 넣는다.
--    레거시 계약(app/schemas/shop.py의 ShopProduct)은 착용 아이템에 detail_url과
--    upright_layer_url을 요구하므로, 이 플래그가 빠지면 구버전 앱의 /shop/products와
--    /inventory가 통째로 500이 된다. get_products/get_inventory가 이 플래그로 걸러낸다.
--
-- 계약도 스키마도 바뀌지 않으므로 서버·앱 배포와 순서 의존이 없다. 단독 적용 가능하고,
-- 재실행해도 안전하다(고정 uuid + ON CONFLICT).
BEGIN;

-- ─────────────────────────────────────────────────────────────
-- 1) 선행 조건
-- ─────────────────────────────────────────────────────────────
DO $$
BEGIN
  -- slot 'hat'의 존재를 대표 상품으로 확인한다 — 20260719가 아직이면 여기서 멈춘다.
  IF NOT EXISTS (
    SELECT 1 FROM public.products
    WHERE product_type = 'cosmetic' AND public_id = 'head_mandarin' AND slot = 'hat'
  ) THEN
    RAISE EXCEPTION 'hat 슬롯이 준비되지 않았다 — 20260719_hat_glasses_rightside.sql을 먼저 적용한다';
  END IF;

  -- public_id는 partial UNIQUE(products_public_id_uq)다. 아래에서 쓸 고정 uuid 범위
  -- (…0205 ~ …0211) 밖의 행이 같은 public_id를 이미 선점했다면, INSERT가 알아보기 힘든
  -- unique_violation으로 죽는다. 먼저 사람이 읽을 수 있는 메시지로 잡는다.
  IF EXISTS (
    SELECT 1 FROM public.products
    WHERE public_id = ANY (ARRAY[
            'head_bucket', 'head_cap', 'head_beret', 'head_bandana',
            'head_towel', 'head_watermelon', 'head_rabbit'])
      AND (id < '00000000-0000-4000-8000-000000000205'::uuid
        OR id > '00000000-0000-4000-8000-000000000211'::uuid)
  ) THEN
    RAISE EXCEPTION '새 모자 public_id를 다른 상품 행이 이미 쓰고 있다 — 수동으로 정리한 뒤 재실행한다';
  END IF;
END;
$$;

-- ─────────────────────────────────────────────────────────────
-- 2) 상품 7종 적재
--    에셋 경로 규칙은 카탈로그 전체와 같다: {public_id}/v{asset_version}/…
--    URL을 손으로 14번 적지 않고 public_id에서 만들어 오타 가능성을 없앤다.
--    sort_order는 슬롯 안에서의 순번이다(기존 head_mandarin=2 뒤에 3부터 붙인다).
-- ─────────────────────────────────────────────────────────────
INSERT INTO public.products (
  id, product_type, public_id, slot, name, name_i18n, price_hay,
  is_subscriber_only, asset_version, assets, is_active, is_v2_only, sort_order
)
SELECT
  hat.id,
  'cosmetic',
  hat.public_id,
  'hat',
  hat.name,
  hat.name_i18n,
  1000,
  false,
  1,
  jsonb_build_object(
    'thumbnail_url', format('%s/%s/v1/thumb.png', bucket.base, hat.public_id),
    'rightside', jsonb_build_object(
      'upright_layer_url', format('%s/%s/v1/rightside/upright.png', bucket.base, hat.public_id)
    )
  ),
  true,
  true,
  hat.sort_order
FROM (VALUES
  ('00000000-0000-4000-8000-000000000205'::uuid, 'head_bucket', '버킷햇',
   '{"ko":"버킷햇","en":"Bucket Hat","ja":"バケットハット"}'::jsonb, 3),
  ('00000000-0000-4000-8000-000000000206'::uuid, 'head_cap', '캡모자',
   '{"ko":"캡모자","en":"Cap","ja":"キャップ"}'::jsonb, 4),
  ('00000000-0000-4000-8000-000000000207'::uuid, 'head_beret', '빵모자',
   '{"ko":"빵모자","en":"Beret","ja":"ベレー帽"}'::jsonb, 5),
  ('00000000-0000-4000-8000-000000000208'::uuid, 'head_bandana', '두건',
   '{"ko":"두건","en":"Bandana","ja":"バンダナ"}'::jsonb, 6),
  ('00000000-0000-4000-8000-000000000209'::uuid, 'head_towel', '수건',
   '{"ko":"수건","en":"Towel","ja":"タオル"}'::jsonb, 7),
  ('00000000-0000-4000-8000-000000000210'::uuid, 'head_watermelon', '수박 모자',
   '{"ko":"수박 모자","en":"Watermelon Hat","ja":"スイカ帽子"}'::jsonb, 8),
  ('00000000-0000-4000-8000-000000000211'::uuid, 'head_rabbit', '토끼 모자',
   '{"ko":"토끼 모자","en":"Bunny Hood","ja":"うさぎ帽子"}'::jsonb, 9)
) AS hat(id, public_id, name, name_i18n, sort_order)
CROSS JOIN (VALUES
  ('https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets')
) AS bucket(base)
ON CONFLICT (id) DO UPDATE
  SET product_type       = EXCLUDED.product_type,
      public_id          = EXCLUDED.public_id,
      name               = EXCLUDED.name,
      name_i18n          = EXCLUDED.name_i18n,
      price_hay          = EXCLUDED.price_hay,
      is_subscriber_only = EXCLUDED.is_subscriber_only,
      asset_version      = EXCLUDED.asset_version,
      assets             = EXCLUDED.assets,
      is_active          = EXCLUDED.is_active,
      is_v2_only         = EXCLUDED.is_v2_only,
      sort_order         = EXCLUDED.sort_order;
      -- slot은 갱신하지 않는다 — user_items의 복합 FK(product_id, equipped_slot)가 걸려 있어
      -- 슬롯 이동은 전용 cutover 마이그레이션에서만 한다. 신규 행은 INSERT가 이미 'hat'을 넣는다.

-- ─────────────────────────────────────────────────────────────
-- 3) 사후 검증 — 하나라도 어긋나면 전체를 되돌린다.
-- ─────────────────────────────────────────────────────────────
DO $$
DECLARE
  v_new_ids uuid[] := ARRAY[
    '00000000-0000-4000-8000-000000000205'::uuid,
    '00000000-0000-4000-8000-000000000206'::uuid,
    '00000000-0000-4000-8000-000000000207'::uuid,
    '00000000-0000-4000-8000-000000000208'::uuid,
    '00000000-0000-4000-8000-000000000209'::uuid,
    '00000000-0000-4000-8000-000000000210'::uuid,
    '00000000-0000-4000-8000-000000000211'::uuid];
  v_count integer;
  v_bad   text;
BEGIN
  SELECT count(*) INTO v_count FROM public.products WHERE id = ANY (v_new_ids);
  IF v_count <> 7 THEN
    RAISE EXCEPTION '모자 7종 중 %건만 적재됐다', v_count;
  END IF;

  -- 슬롯·활성·가격·버전과 v2 전용 플래그.
  SELECT string_agg(public_id, ', ') INTO v_bad
  FROM public.products
  WHERE id = ANY (v_new_ids)
    AND NOT (product_type = 'cosmetic' AND slot = 'hat' AND is_active
             AND is_v2_only AND asset_version = 1 AND price_hay = 1000
             AND is_subscriber_only = false);
  IF v_bad IS NOT NULL THEN
    RAISE EXCEPTION '상품 속성이 기대와 다르다: %', v_bad;
  END IF;

  -- v2 착용 계약: thumbnail_url + rightside.upright_layer_url만 있어야 한다.
  -- detail_url이나 구 자세 upright_layer_url이 섞이면 ShopProductV2 검증이 500을 낸다.
  SELECT string_agg(public_id, ', ') INTO v_bad
  FROM public.products
  WHERE id = ANY (v_new_ids)
    AND NOT (assets ? 'thumbnail_url'
             AND (assets->'rightside') ? 'upright_layer_url'
             AND NOT (assets ? 'detail_url')
             AND NOT (assets ? 'upright_layer_url')
             AND NOT (assets ? 'scene'));
  IF v_bad IS NOT NULL THEN
    RAISE EXCEPTION 'assets가 v2 착용 계약과 다르다: %', v_bad;
  END IF;

  -- 에셋 URL이 실제 업로드한 경로({public_id}/v1/…)를 가리키는지.
  SELECT string_agg(public_id, ', ') INTO v_bad
  FROM public.products
  WHERE id = ANY (v_new_ids)
    AND NOT (assets->>'thumbnail_url' = format(
               'https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/%s/v1/thumb.png',
               public_id)
             AND assets->'rightside'->>'upright_layer_url' = format(
               'https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/%s/v1/rightside/upright.png',
               public_id));
  IF v_bad IS NOT NULL THEN
    RAISE EXCEPTION '에셋 URL이 규칙과 다르다: %', v_bad;
  END IF;

  -- 활성 hat 슬롯은 기존 head_mandarin 1종 + 신규 7종 = 8종이어야 한다.
  SELECT count(*) INTO v_count
  FROM public.products
  WHERE product_type = 'cosmetic' AND is_active AND slot = 'hat';
  IF v_count <> 8 THEN
    RAISE EXCEPTION '활성 hat 상품이 8종이 아니라 %종이다', v_count;
  END IF;
END;
$$;

COMMIT;
