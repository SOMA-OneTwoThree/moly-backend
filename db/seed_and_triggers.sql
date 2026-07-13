-- moly-backend 시드 + 가입 트리거 (2026-07-08)
-- 실행: db/apply.py 계열로 dry-run(ROLLBACK) → --commit
-- 멱등: 재실행해도 안전(ON CONFLICT / CREATE OR REPLACE / DROP TRIGGER IF EXISTS).

-- ─────────────────────────────────────────────────────────────
-- 1. 가입 트리거 — auth.users INSERT 시 초기 상태 자동 세팅 (ERD §3.2)
--    profiles(trial_ends_at = 가입시각 + 48h) + 기본 지급 아이템 3종(user_items,
--    source='admin_grant' — 집·운동 배경, 선글라스. 장착은 안 함 = 기본 몰리/기본 배경 시작)
--    + 기본 루틴 2개(이불 정리하기·물 마시기, 매일/주7회, 리마인더 off).
--    SECURITY DEFINER: auth 컨텍스트에서 public 삽입 위해. search_path 고정(주입 방어).
--    ⚠️ moly-auth self-heal(ensureProfile)로 profiles가 생기는 경로는 기본 지급이 없음 —
--       auth.users를 함께 리셋하는 전제(가입은 항상 이 트리거를 지남).
-- ─────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  INSERT INTO public.profiles (id, trial_ends_at)
  VALUES (NEW.id, NEW.created_at + interval '48 hours')
  ON CONFLICT (id) DO NOTHING;
  -- 기본 지급 3종: 집(…101)·운동(…102) 배경, 선글라스(…201) — products 시드의 고정 uuid
  INSERT INTO public.user_items (user_id, product_id, source)
  SELECT NEW.id, p.id, 'admin_grant'
  FROM public.products p
  WHERE p.id IN ('00000000-0000-4000-8000-000000000101',
                 '00000000-0000-4000-8000-000000000102',
                 '00000000-0000-4000-8000-000000000201')
  ON CONFLICT (user_id, product_id) DO NOTHING;
  -- 기본 루틴 2개 — 매일(주 7회), 요일 지정 없음, 리마인더 off
  INSERT INTO public.routines (user_id, name, frequency_per_week, reminder_enabled)
  VALUES (NEW.id, '이불 정리하기', 7, false),
         (NEW.id, '물 마시기', 7, false);
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
-- 3. products (product_type='cosmetic') — 꾸미기 상품 6종: 배경 2 · 머리 2 · 목 2 (2026-07-12)
--    자연키가 없어 id를 고정 uuid로 박아 멱등(재실행 = 갱신).
--    이미지: Storage `shop-assets` 버킷 public URL(시크릿 아님 — API 응답으로 클라 전송).
--    배경 낮/밤·썸네일은 에셋 미확정으로 당분간 동일 이미지 —
--    확정 시 새 파일(_v2) 업로드 후 assets URL만 UPDATE(캐시 무효화 겸용).
-- ─────────────────────────────────────────────────────────────
INSERT INTO public.products (id, product_type, slot, name, price_hay, is_subscriber_only, assets, is_active, sort_order) VALUES
  ('00000000-0000-4000-8000-000000000101', 'cosmetic', 'background', '집', 4000, false,
   '{"day":       "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/background/home_v1.png",
     "night":     "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/background/home_v1.png",
     "thumbnail": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/background/home_v1.png"}',
   true, 1),
  ('00000000-0000-4000-8000-000000000102', 'cosmetic', 'background', '운동', 4000, false,
   '{"day":       "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/background/gym_v1.png",
     "night":     "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/background/gym_v1.png",
     "thumbnail": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/background/gym_v1.png"}',
   true, 2),
  ('00000000-0000-4000-8000-000000000201', 'cosmetic', 'head', '선글라스', 1000, false,
   '{"head_layer": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head/sunglasses_v1.png",
     "thumbnail":  "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head/sunglasses_v1.png"}',
   true, 1),
  ('00000000-0000-4000-8000-000000000202', 'cosmetic', 'head', '귤', 1000, false,
   '{"head_layer": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head/mandarin_v1.png",
     "thumbnail":  "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head/mandarin_v1.png"}',
   true, 2),
  ('00000000-0000-4000-8000-000000000301', 'cosmetic', 'neck', '사원증', 1000, false,
   '{"neck_layer": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck/card_v1.png",
     "thumbnail":  "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck/card_v1.png"}',
   true, 1),
  ('00000000-0000-4000-8000-000000000302', 'cosmetic', 'neck', '목도리', 1000, false,
   '{"neck_layer": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck/muffler_v1.png",
     "thumbnail":  "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck/muffler_v1.png"}',
   true, 2)
ON CONFLICT (id) DO UPDATE
  SET product_type       = EXCLUDED.product_type,
      slot               = EXCLUDED.slot,
      name               = EXCLUDED.name,
      price_hay          = EXCLUDED.price_hay,
      is_subscriber_only = EXCLUDED.is_subscriber_only,
      assets             = EXCLUDED.assets,
      is_active          = EXCLUDED.is_active,
      sort_order         = EXCLUDED.sort_order;
