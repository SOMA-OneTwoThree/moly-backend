-- Appearance v2 breaking cutover.
-- 선행 조건:
--   1) 20260713_appearance_v2_expand.sql 적용
--   2) db/seed_and_triggers.sql 적용 — v2 assets/asset_version을 적재하고 6종을 active로 만든다.
--      (시드는 slot을 갱신하지 않는다. background → theme 전환은 이 파일이 복합 FK를 내린 뒤 한다.)
--   3) scripts/verify_appearance_assets.py 성공
-- 시드 적용 시점부터 이 파일 커밋까지는 가입이 중단된다(bootstrap_user가 slot='theme'를 요구).
-- 유지보수 창에서 두 서버 배포 직전에 연속으로 실행한다.
BEGIN;

DO $$
DECLARE
  invalid_count integer;
  required_count integer;
BEGIN
  SELECT count(*) INTO required_count
  FROM public.products
  WHERE product_type = 'cosmetic' AND is_active = true
    AND (
      (public_id IN ('theme_default', 'theme_workout') AND slot IN ('background', 'theme'))
      OR (public_id = 'head_sunglasses' AND slot = 'head')
    );
  IF required_count <> 3 THEN
    RAISE EXCEPTION 'required appearance products are not active';
  END IF;

  SELECT count(*) INTO invalid_count
  FROM public.products
  WHERE product_type = 'cosmetic' AND is_active = true AND (
    public_id IS NULL OR asset_version IS NULL OR asset_version < 1 OR assets IS NULL
    OR NOT (assets ? 'thumbnail_url') OR NOT (assets ? 'detail_url')
    OR CASE
      WHEN slot IN ('background', 'theme') THEN
        NOT (assets ? 'scene')
        OR NOT COALESCE((assets->'scene'->'canvas'->>'width')::integer = 393, false)
        OR NOT COALESCE((assets->'scene'->'canvas'->>'height')::integer = 852, false)
        OR NOT (assets->'scene' ? 'character_frame')
        OR NOT (assets->'scene' ? 'character_url')
        OR COALESCE(jsonb_typeof(assets->'scene'->'layers'), '') <> 'array'
        OR CASE
          WHEN jsonb_typeof(assets->'scene'->'layers') = 'array'
          THEN jsonb_array_length(assets->'scene'->'layers') = 0
          ELSE true
        END
        OR assets ? 'upright_layer_url'
      ELSE
        NOT (assets ? 'upright_layer_url') OR assets ? 'scene'
    END
  );
  IF invalid_count <> 0 THEN
    RAISE EXCEPTION '% active cosmetics do not satisfy appearance v2', invalid_count;
  END IF;

  IF EXISTS (
    SELECT 1
    FROM public.products p,
         LATERAL jsonb_array_elements(p.assets->'scene'->'layers') layer
    WHERE p.product_type = 'cosmetic' AND p.is_active = true
      AND p.slot IN ('background', 'theme')
      AND NOT (layer ? 'day_url')
  ) THEN
    RAISE EXCEPTION 'every theme layer requires day_url';
  END IF;
END;
$$;

-- 복합 FK를 잠시 내려 product/user_items 슬롯을 같은 트랜잭션에서 변경한다.
ALTER TABLE public.user_items DROP CONSTRAINT IF EXISTS user_items_product_slot_fk;
ALTER TABLE public.user_items DROP CONSTRAINT IF EXISTS user_items_equipped_slot_check;
ALTER TABLE public.products DROP CONSTRAINT IF EXISTS products_slot_check;
ALTER TABLE public.products DROP CONSTRAINT IF EXISTS products_cosmetic_ck;

-- 구독 권한만으로 만들어진 비소유 장착은 새 계약에서 허용하지 않는다.
DELETE FROM public.user_items WHERE source = 'subscription';

UPDATE public.user_items SET equipped_slot = 'theme' WHERE equipped_slot = 'background';
UPDATE public.products SET slot = 'theme' WHERE slot = 'background';
UPDATE public.products SET price_hay = NULL WHERE public_id = 'theme_default';
-- 구독 전용 꾸미기 폐지 — 카탈로그에 뜨지만 구매만 403이 되는 상품을 남기지 않는다.
UPDATE public.products SET is_subscriber_only = false WHERE product_type = 'cosmetic';

ALTER TABLE public.products ADD CONSTRAINT products_slot_check
  CHECK (slot IN ('theme','head','neck','body'));
ALTER TABLE public.products ADD CONSTRAINT products_cosmetic_ck CHECK (
  product_type <> 'cosmetic' OR (
    public_id IS NOT NULL AND slot IS NOT NULL
    AND hay_amount IS NULL AND app_store_product_id IS NULL AND price_krw IS NULL
    AND is_subscriber_only = false
    AND (
      is_active = false
      OR (asset_version IS NOT NULL AND asset_version >= 1 AND assets IS NOT NULL)
    )
  )
);

ALTER TABLE public.user_items ADD CONSTRAINT user_items_equipped_slot_check
  CHECK (equipped_slot IN ('theme','head','neck','body'));
ALTER TABLE public.user_items ADD CONSTRAINT user_items_product_slot_fk
  FOREIGN KEY (product_id, equipped_slot) REFERENCES public.products(id, slot);

-- 기존 사용자에게는 theme_default만 예외적으로 소급 지급한다.
INSERT INTO public.user_items (user_id, product_id, source)
SELECT profile.id, default_theme.id, 'admin_grant'
FROM public.profiles profile
CROSS JOIN public.products default_theme
WHERE default_theme.public_id = 'theme_default' AND default_theme.is_active = true
ON CONFLICT (user_id, product_id) DO NOTHING;

UPDATE public.user_items default_item
SET equipped_slot = 'theme', equipped_at = COALESCE(default_item.equipped_at, now())
FROM public.products default_product
WHERE default_item.product_id = default_product.id
  AND default_product.public_id = 'theme_default'
  AND NOT EXISTS (
    SELECT 1 FROM public.user_items equipped
    WHERE equipped.user_id = default_item.user_id AND equipped.equipped_slot = 'theme'
  );

-- 신규 가입 트리거와 moly-auth self-heal이 공유하는 원자적 부트스트랩.
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
  WHERE product_type = 'cosmetic' AND is_active = true
    AND (
      (public_id IN ('theme_default', 'theme_workout') AND slot = 'theme')
      OR (public_id = 'head_sunglasses' AND slot = 'head')
    );
  IF v_required_count <> 3 THEN
    RAISE EXCEPTION 'appearance bootstrap products are not ready';
  END IF;

  INSERT INTO public.profiles (id, trial_ends_at)
  VALUES (p_user_id, p_created_at + interval '48 hours')
  ON CONFLICT (id) DO NOTHING;
  GET DIAGNOSTICS v_profile_created = ROW_COUNT;

  INSERT INTO public.user_items (user_id, product_id, source)
  SELECT p_user_id, p.id, 'admin_grant'
  FROM public.products p
  WHERE p.product_type = 'cosmetic' AND p.is_active = true
    AND (
      (p.public_id IN ('theme_default', 'theme_workout') AND p.slot = 'theme')
      OR (p.public_id = 'head_sunglasses' AND p.slot = 'head')
    )
  ON CONFLICT (user_id, product_id) DO NOTHING;

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
    INSERT INTO public.routines (user_id, name, frequency_per_week, reminder_enabled)
    VALUES (p_user_id, '이불 정리하기', 7, false),
           (p_user_id, '물 마시기', 7, false);
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

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM public.profiles profile
    WHERE NOT EXISTS (
      SELECT 1 FROM public.user_items item
      JOIN public.products product ON product.id = item.product_id
      WHERE item.user_id = profile.id AND item.equipped_slot = 'theme'
        AND product.slot = 'theme' AND product.is_active = true
    )
  ) THEN
    RAISE EXCEPTION 'at least one user has no active equipped theme';
  END IF;

  IF EXISTS (SELECT 1 FROM public.user_items WHERE source = 'subscription') THEN
    RAISE EXCEPTION 'legacy subscription equipment remains';
  END IF;
END;
$$;

COMMIT;
