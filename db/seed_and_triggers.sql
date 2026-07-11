-- moly-backend 시드 + 가입 트리거 (2026-07-08)
-- 실행: db/apply.py 계열로 dry-run(ROLLBACK) → --commit
-- 멱등: 재실행해도 안전(ON CONFLICT / CREATE OR REPLACE / DROP TRIGGER IF EXISTS).

-- ─────────────────────────────────────────────────────────────
-- 1. 가입 트리거 — auth.users INSERT 시 public.profiles 자동 생성 (ERD §3.2)
--    trial_ends_at = 가입시각 + 48h(체험 2일, 절대시각 정책). 나머지는 컬럼 기본값.
--    SECURITY DEFINER: auth 컨텍스트에서 public 삽입 위해. search_path 고정(주입 방어).
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
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- (기존 auth.users backfill 안 함 — 사용자 결정 2026-07-08. 트리거 이후 신규 가입만 생성.)

-- ─────────────────────────────────────────────────────────────
-- 2. hay_packs — 건초 IAP 상품 3종 (App Store Connect 등록 product_id)
--    가격: 300/₩1,500 · 1,500/₩6,500 · 3,000/₩10,000 (확정 정책)
-- ─────────────────────────────────────────────────────────────
INSERT INTO public.hay_packs (app_store_product_id, hay_amount, price_krw, is_active, sort_order) VALUES
  ('com.geniusjun.moly.hay.300',   300,   1500, true, 1),
  ('com.geniusjun.moly.hay.1500', 1500,   6500, true, 2),
  ('com.geniusjun.moly.hay.3000', 3000,  10000, true, 3)
ON CONFLICT (app_store_product_id) DO UPDATE
  SET hay_amount = EXCLUDED.hay_amount,
      price_krw  = EXCLUDED.price_krw,
      is_active  = EXCLUDED.is_active,
      sort_order = EXCLUDED.sort_order;

-- ─────────────────────────────────────────────────────────────
-- 3. shop_items — 상점 상품 6종: 배경 2 · 머리 2 · 목 2 (2026-07-12)
--    자연키가 없어 id를 고정 uuid로 박아 멱등(재실행 = 갱신).
--    이미지: Storage `shop-assets` 버킷 public URL(시크릿 아님 — API 응답으로 클라 전송).
--    배경 낮/밤·썸네일은 에셋 미확정으로 당분간 동일 이미지 —
--    확정 시 새 파일(_v2) 업로드 후 assets URL만 UPDATE(캐시 무효화 겸용).
-- ─────────────────────────────────────────────────────────────
INSERT INTO public.shop_items (id, slot, name, price_hay, is_subscriber_only, assets, is_active, sort_order) VALUES
  ('00000000-0000-4000-8000-000000000101', 'background', '집', 4000, false,
   '{"day":       "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/background/home_v1.png",
     "night":     "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/background/home_v1.png",
     "thumbnail": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/background/home_v1.png"}',
   true, 1),
  ('00000000-0000-4000-8000-000000000102', 'background', '운동', 4000, false,
   '{"day":       "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/background/gym_v1.png",
     "night":     "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/background/gym_v1.png",
     "thumbnail": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/background/gym_v1.png"}',
   true, 2),
  ('00000000-0000-4000-8000-000000000201', 'head', '선글라스', 1000, false,
   '{"head_layer": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head/sunglasses_v1.png",
     "thumbnail":  "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head/sunglasses_v1.png"}',
   true, 1),
  ('00000000-0000-4000-8000-000000000202', 'head', '귤', 1000, false,
   '{"head_layer": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head/mandarin_v1.png",
     "thumbnail":  "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head/mandarin_v1.png"}',
   true, 2),
  ('00000000-0000-4000-8000-000000000301', 'neck', '사원증', 1000, false,
   '{"neck_layer": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck/card_v1.png",
     "thumbnail":  "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck/card_v1.png"}',
   true, 1),
  ('00000000-0000-4000-8000-000000000302', 'neck', '목도리', 1000, false,
   '{"neck_layer": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck/muffler_v1.png",
     "thumbnail":  "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck/muffler_v1.png"}',
   true, 2)
ON CONFLICT (id) DO UPDATE
  SET slot               = EXCLUDED.slot,
      name               = EXCLUDED.name,
      price_hay          = EXCLUDED.price_hay,
      is_subscriber_only = EXCLUDED.is_subscriber_only,
      assets             = EXCLUDED.assets,
      is_active          = EXCLUDED.is_active,
      sort_order         = EXCLUDED.sort_order;
