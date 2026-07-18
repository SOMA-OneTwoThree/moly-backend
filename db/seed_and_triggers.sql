-- moly-backend 시드 + 가입 트리거 (2026-07-13 appearance v2)
-- 실행: db/apply.py 계열로 dry-run(ROLLBACK) → --commit
-- 멱등: 재실행해도 안전(ON CONFLICT / CREATE OR REPLACE / DROP TRIGGER IF EXISTS).

-- ─────────────────────────────────────────────────────────────
-- 1. 원자적 계정 부트스트랩 — 가입 트리거와 moly-auth self-heal이 함께 호출.
--    필수 활성 상품이 없으면 프로필만 생성되는 불완전 계정을 허용하지 않는다.
-- ─────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.bootstrap_user(
  p_user_id uuid,
  p_created_at timestamptz DEFAULT now()
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_required_count integer;
  v_profile_created integer;
BEGIN
  SELECT count(*) INTO v_required_count
  FROM public.products
  WHERE product_type = 'cosmetic'
    AND is_active = true
    AND (
      (public_id IN ('theme_default', 'theme_workout') AND slot = 'theme')
      OR (public_id = 'head_sunglasses' AND slot = 'head')
    );

  IF v_required_count <> 3 THEN
    RAISE EXCEPTION
      'appearance bootstrap products are not ready: % of 3 active (§3 상품 시드를 먼저 적용하라)',
      v_required_count;
  END IF;

  INSERT INTO public.profiles (id, trial_ends_at)
  VALUES (p_user_id, p_created_at + interval '48 hours')
  ON CONFLICT (id) DO NOTHING;
  GET DIAGNOSTICS v_profile_created = ROW_COUNT;

  INSERT INTO public.user_items (user_id, product_id, source)
  SELECT p_user_id, p.id, 'admin_grant'
  FROM public.products p
  WHERE p.product_type = 'cosmetic'
    AND p.is_active = true
    AND (
      (p.public_id IN ('theme_default', 'theme_workout') AND p.slot = 'theme')
      OR (p.public_id = 'head_sunglasses' AND p.slot = 'head')
    )
  ON CONFLICT (user_id, product_id) DO NOTHING;

  -- 기존 장착 테마가 없을 때만 기본 테마를 장착한다.
  UPDATE public.user_items default_item
  SET equipped_slot = 'theme', equipped_at = COALESCE(default_item.equipped_at, now())
  FROM public.products default_product
  WHERE default_item.user_id = p_user_id
    AND default_item.product_id = default_product.id
    AND default_product.public_id = 'theme_default'
    AND NOT EXISTS (
      SELECT 1 FROM public.user_items equipped
      WHERE equipped.user_id = p_user_id AND equipped.equipped_slot = 'theme'
    );

  IF v_profile_created = 1 THEN
    INSERT INTO public.routines (user_id, name, frequency_per_week, days_of_week, reminder_enabled)
    VALUES (p_user_id, '이불 정리하기', 7, '{1,2,3,4,5,6,7}', false),
           (p_user_id, '물 마시기', 7, '{1,2,3,4,5,6,7}', false);
  END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.bootstrap_user(uuid, timestamptz) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.bootstrap_user(uuid, timestamptz) TO service_role;

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  PERFORM public.bootstrap_user(NEW.id, NEW.created_at);
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- (기존 auth.users backfill 안 함 — 사용자 결정 2026-07-08. 트리거 이후 신규 가입만 생성.)

-- ─────────────────────────────────────────────────────────────
-- 2. products (product_type='hay_pack') — 건초 IAP 상품 3종 (App Store Connect 등록 product_id)
--    가격: 300/₩1,500 · 1,500/₩6,500 · 3,000/₩10,000 (확정 정책)
-- ─────────────────────────────────────────────────────────────
INSERT INTO public.products (product_type, name, hay_amount, price_krw, app_store_product_id, is_active, sort_order) VALUES
  ('hay_pack', '건초 300',   300,   1500, 'com.geniusjun.moly.hay.300',  true, 1),
  ('hay_pack', '건초 1500', 1500,   6500, 'com.geniusjun.moly.hay.1500', true, 2),
  ('hay_pack', '건초 3000', 3000,  10000, 'com.geniusjun.moly.hay.3000', true, 3)
ON CONFLICT (app_store_product_id) DO UPDATE
  SET name       = EXCLUDED.name,
      hay_amount = EXCLUDED.hay_amount,
      price_krw  = EXCLUDED.price_krw,
      is_active  = EXCLUDED.is_active,
      sort_order = EXCLUDED.sort_order;

-- ─────────────────────────────────────────────────────────────
-- 3. products (cosmetic) — appearance v2 꾸미기 9종: 테마 2 · 머리 3 · 목 3 · 몸 1
--    자연키가 없어 id를 고정 uuid로 박아 멱등(재실행 = 갱신).
--    에셋: Storage `shop-assets` 버킷 public URL. 경로 규칙은 {public_id}/v{asset_version}/…
--    — 파일 내용이 바뀌면 asset_version과 URL을 함께 올린다(iOS는 URL 전체를 캐시 키로 씀).
--    scene(레이어 좌표·z-index·character_frame)은 iOS RoomTheme.swift의 번들 폴백과 같은 값이다.
--    한쪽을 바꾸면 다른 쪽도 바꿔야 원격 테마와 폴백이 어긋나지 않는다.
-- ─────────────────────────────────────────────────────────────
INSERT INTO public.products (
  id, product_type, public_id, slot, name, price_hay, is_subscriber_only,
  asset_version, assets, is_active, sort_order
) VALUES
  ('00000000-0000-4000-8000-000000000101', 'cosmetic', 'theme_default', 'theme', '집', NULL, false, 1,
   '{"thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/thumb.png",
     "detail_url":    "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/detail.png",
     "scene": {
       "canvas": {"width": 393, "height": 852},
       "character_frame": {"x": 51, "y": 338.8, "width": 171, "height": 85.2},
       "character_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/character.png",
       "layers": [
         {"id": "background", "z_index": 0, "frame": {"x": 0, "y": 0, "width": 393, "height": 852},
          "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/background-day.png"},
         {"id": "player", "z_index": 10, "frame": {"x": 255.5, "y": 330.1, "width": 137.2, "height": 128.8},
          "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/player-day.png"},
         {"id": "sofa", "z_index": 20, "frame": {"x": 3.7, "y": 341.4, "width": 268.5, "height": 129.3},
          "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/sofa-day.png"},
         {"id": "table", "z_index": 30, "frame": {"x": 38, "y": 543, "width": 318.3, "height": 145.3},
          "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/table-day.png"},
         {"id": "clock", "z_index": 40, "frame": {"x": 74.2, "y": 157.3, "width": 63.7, "height": 64.6},
          "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/clock-day.png"},
         {"id": "window", "z_index": 50, "frame": {"x": 200, "y": 111.6, "width": 151.1, "height": 157},
          "day_url":   "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/window-day.png",
          "night_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/window-night.png"}
       ]
     }}',
   true, 1),
  ('00000000-0000-4000-8000-000000000102', 'cosmetic', 'theme_workout', 'theme', '운동', 4000, false, 1,
   '{"thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/thumb.png",
     "detail_url":    "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/detail.png",
     "scene": {
       "canvas": {"width": 393, "height": 852},
       "character_frame": {"x": 98.1, "y": 466.1, "width": 196.8, "height": 182},
       "character_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/character.png",
       "layers": [
         {"id": "background", "z_index": 0, "frame": {"x": 0, "y": 0, "width": 393, "height": 852},
          "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/background-day.png"},
         {"id": "photo", "z_index": 10, "frame": {"x": 39, "y": 150, "width": 96, "height": 99.3},
          "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/photo-day.png"},
         {"id": "window", "z_index": 20, "frame": {"x": 192, "y": 155, "width": 171, "height": 113},
          "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/window-day.png"},
         {"id": "light", "z_index": 30, "frame": {"x": 234, "y": 0, "width": 61.5, "height": 130},
          "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/light-day.png"},
         {"id": "dumbbell", "z_index": 40, "frame": {"x": 25, "y": 350, "width": 125, "height": 98},
          "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/dumbbell-day.png"},
         {"id": "treadmill", "z_index": 50, "frame": {"x": 200, "y": 286.9, "width": 155, "height": 214.7},
          "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/treadmill-day.png"},
         {"id": "mat", "z_index": 60, "frame": {"x": 72, "y": 584.8, "width": 233, "height": 91.3},
          "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/mat-day.png"},
         {"id": "water", "z_index": 70, "frame": {"x": 315, "y": 585.2, "width": 33.2, "height": 83.6},
          "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/water-day.png"}
       ]
     }}',
   true, 2),
  ('00000000-0000-4000-8000-000000000201', 'cosmetic', 'head_sunglasses', 'head', '선글라스', 1000, false, 2,
   '{"thumbnail_url":     "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_sunglasses/v2/thumb.png",
     "detail_url":        "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_sunglasses/v2/detail.png",
     "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_sunglasses/v2/upright.png"}',
   true, 1),
  ('00000000-0000-4000-8000-000000000202', 'cosmetic', 'head_mandarin', 'head', '귤', 1000, false, 2,
   '{"thumbnail_url":     "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_mandarin/v2/thumb.png",
     "detail_url":        "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_mandarin/v2/detail.png",
     "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_mandarin/v2/upright.png"}',
   true, 2),
  ('00000000-0000-4000-8000-000000000301', 'cosmetic', 'neck_employee_badge', 'neck', '사원증', 1000, false, 2,
   '{"thumbnail_url":     "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_employee_badge/v2/thumb.png",
     "detail_url":        "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_employee_badge/v2/detail.png",
     "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_employee_badge/v2/upright.png"}',
   true, 1),
  ('00000000-0000-4000-8000-000000000302', 'cosmetic', 'neck_muffler', 'neck', '목도리', 1000, false, 2,
   '{"thumbnail_url":     "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_muffler/v2/thumb.png",
     "detail_url":        "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_muffler/v2/detail.png",
     "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_muffler/v2/upright.png"}',
   true, 2),
  ('00000000-0000-4000-8000-000000000203', 'cosmetic', 'head_suncream', 'head', '선크림', 1000, false, 1,
   '{"thumbnail_url":     "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_suncream/v1/thumb.png",
     "detail_url":        "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_suncream/v1/detail.png",
     "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_suncream/v1/upright.png"}',
   true, 3),
  ('00000000-0000-4000-8000-000000000303', 'cosmetic', 'neck_shell', 'neck', '조개 목걸이', 1000, false, 1,
   '{"thumbnail_url":     "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_shell/v1/thumb.png",
     "detail_url":        "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_shell/v1/detail.png",
     "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_shell/v1/upright.png"}',
   true, 3),
  ('00000000-0000-4000-8000-000000000401', 'cosmetic', 'clothes_hawaiianpants', 'body', '하와이안 바지', 1000, false, 1,
   '{"thumbnail_url":     "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/clothes_hawaiianpants/v1/thumb.png",
     "detail_url":        "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/clothes_hawaiianpants/v1/detail.png",
     "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/clothes_hawaiianpants/v1/upright.png"}',
   true, 1)
ON CONFLICT (id) DO UPDATE
  SET product_type       = EXCLUDED.product_type,
      public_id          = EXCLUDED.public_id,
      name               = EXCLUDED.name,
      price_hay          = EXCLUDED.price_hay,
      is_subscriber_only = EXCLUDED.is_subscriber_only,
      is_active          = EXCLUDED.is_active,
      sort_order         = EXCLUDED.sort_order,
      -- slot은 갱신하지 않는다 — 장착 행의 복합 FK가 걸려 있어 cutover 안에서만 바꿀 수 있다.
      -- 에셋은 버전이 올라갈 때만 덮는다 — 나중에 올린 최종 아트를 재시드가 되돌리지 않는다.
      asset_version      = GREATEST(EXCLUDED.asset_version, COALESCE(public.products.asset_version, 0)),
      assets             = CASE
        WHEN EXCLUDED.asset_version > COALESCE(public.products.asset_version, 0)
        THEN EXCLUDED.assets
        ELSE public.products.assets
      END;
