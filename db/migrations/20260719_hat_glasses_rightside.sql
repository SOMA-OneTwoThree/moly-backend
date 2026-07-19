-- head 슬롯을 hat/glasses로 분리 + 착용 아이템에 rightside(오른쪽으로 튼 새 자세) 레이어 추가.
-- 선행 조건: 20260713_appearance_v2_cutover.sql 적용(slot 4종 체계 theme/head/neck/body).
-- 유지보수 창에서 새 서버 배포 직전에 실행한다 — 구 서버는 slot='glasses'/'hat'을 만나면
-- /shop/products가 500이 된다(레거시 slot Literal 위반). 새 서버는 hat/glasses를 레거시 경로에서
-- 'head'로 투영해 구버전 앱을 계속 지원한다.
BEGIN;

DO $$
DECLARE
  head_total integer;
  head_known integer;
BEGIN
  SELECT count(*) INTO head_total
  FROM public.products
  WHERE product_type = 'cosmetic' AND is_active = true AND slot = 'head';

  SELECT count(*) INTO head_known
  FROM public.products
  WHERE product_type = 'cosmetic' AND is_active = true AND slot = 'head'
    AND public_id IN ('head_sunglasses', 'head_mandarin', 'head_suncream');

  IF head_total <> 3 OR head_known <> 3 THEN
    RAISE EXCEPTION
      'expected exactly the 3 known head products, found % active head (% known)',
      head_total, head_known;
  END IF;
END;
$$;

-- 복합 FK를 잠시 내려 product/user_items 슬롯을 같은 트랜잭션에서 변경한다.
ALTER TABLE public.user_items DROP CONSTRAINT IF EXISTS user_items_product_slot_fk;
ALTER TABLE public.user_items DROP CONSTRAINT IF EXISTS user_items_equipped_slot_check;
ALTER TABLE public.products DROP CONSTRAINT IF EXISTS products_slot_check;

-- 장착 행을 상품의 새 슬롯으로 옮긴다(사용자당 head 1행이라 슬롯 unique 충돌 없음).
UPDATE public.user_items item
SET equipped_slot = CASE product.public_id
    WHEN 'head_mandarin' THEN 'hat'
    ELSE 'glasses'
  END
FROM public.products product
WHERE item.product_id = product.id AND item.equipped_slot = 'head';

UPDATE public.products SET slot = 'hat'     WHERE public_id = 'head_mandarin';
UPDATE public.products SET slot = 'glasses' WHERE public_id IN ('head_sunglasses', 'head_suncream');

ALTER TABLE public.products ADD CONSTRAINT products_slot_check
  CHECK (slot IN ('theme','hat','glasses','neck','body'));
ALTER TABLE public.user_items ADD CONSTRAINT user_items_equipped_slot_check
  CHECK (equipped_slot IN ('theme','hat','glasses','neck','body'));
ALTER TABLE public.user_items ADD CONSTRAINT user_items_product_slot_fk
  FOREIGN KEY (product_id, equipped_slot) REFERENCES public.products(id, slot);

-- 착용 아이템에 rightside 자세 레이어를 추가한다 — 기존 upright URL에서 /rightside/ 경로를 파생.
-- 테마는 자세 변경 대상이 아니라 건드리지 않는다. asset_version은 올리지 않아 구 자세 URL은 불변이다.
UPDATE public.products
SET assets = assets || jsonb_build_object(
  'rightside', jsonb_build_object(
    'upright_layer_url',
    regexp_replace(assets->>'upright_layer_url', '/upright\.png$', '/rightside/upright.png')
  ))
WHERE product_type = 'cosmetic' AND is_active = true
  AND assets ? 'upright_layer_url'
  AND NOT (assets ? 'rightside');

-- 신규 가입 부트스트랩이 요구하는 필수 상품 조건을 새 슬롯에 맞춘다(head_sunglasses → glasses).
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
      OR (public_id = 'head_sunglasses' AND slot = 'glasses')
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
      OR (p.public_id = 'head_sunglasses' AND p.slot = 'glasses')
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
    INSERT INTO public.routines (user_id, name, frequency_per_week, days_of_week, reminder_enabled)
    VALUES (p_user_id, '이불 정리하기', 7, '{1,2,3,4,5,6,7}', false),
           (p_user_id, '물 마시기', 7, '{1,2,3,4,5,6,7}', false);
  END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.bootstrap_user(uuid, timestamptz) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.bootstrap_user(uuid, timestamptz) TO service_role;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM public.products WHERE product_type = 'cosmetic' AND slot = 'head'
  ) THEN
    RAISE EXCEPTION 'head slot still present after migration';
  END IF;
  IF EXISTS (SELECT 1 FROM public.user_items WHERE equipped_slot = 'head') THEN
    RAISE EXCEPTION 'head equipped_slot still present after migration';
  END IF;
  IF EXISTS (
    SELECT 1 FROM public.products
    WHERE product_type = 'cosmetic' AND is_active = true AND assets ? 'upright_layer_url'
      AND NOT ((assets->'rightside') ? 'upright_layer_url')
  ) THEN
    RAISE EXCEPTION 'active wearable is missing rightside.upright_layer_url';
  END IF;
END;
$$;

COMMIT;
