-- 적용: python db/apply.py db/migrations/20260729_retire_workout_theme.sql --commit
-- SOMA-390: 운동 테마(theme_workout) 폐지 — 전 유저 홈 테마(theme_default)로.
--   운동 테마는 나중에 보완해 다른 테마로 신규 추가 예정 → 하드 삭제 대신 비활성화(되돌리기 가능,
--   보유행 user_items는 비활성이라 인벤토리서 자동 제외).
-- 순서 중요: (1)bootstrap 먼저 고쳐야 신규가입 안 깨짐, (2)장착자 재장착 후 (3)비활성화
--   (_equipment_dto는 장착 상품이 비활성이면 500이라 재장착이 비활성화보다 선행).
BEGIN;

-- 1) bootstrap_user: 지급·필수검사에서 theme_workout 제거 → theme_default + 선글라스 2종만.
--    (안 고치면 required_count<>2로 RAISE → 신규 가입 실패)
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
      (public_id = 'theme_default' AND slot = 'theme')
      OR (public_id = 'head_sunglasses' AND slot = 'glasses')
    );
  IF v_required_count <> 2 THEN
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
      (p.public_id = 'theme_default' AND p.slot = 'theme')
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
    INSERT INTO public.routines (user_id, name, name_i18n, frequency_per_week, days_of_week, reminder_enabled)
    VALUES
      (p_user_id, '이불 정리하기',
       '{"ko":"이불 정리하기","en":"Make the bed","ja":"布団を整える"}'::jsonb,
       7, '{1,2,3,4,5,6,7}', false),
      (p_user_id, '물 마시기',
       '{"ko":"물 마시기","en":"Drink water","ja":"水を飲む"}'::jsonb,
       7, '{1,2,3,4,5,6,7}', false);
  END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.bootstrap_user(uuid, timestamptz) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.bootstrap_user(uuid, timestamptz) TO service_role;

-- 2) theme_workout 장착자 → theme_default 재장착.
--    2a. 운동 테마 장착 해제(그 유저의 theme 슬롯을 비운다).
UPDATE public.user_items ui
SET equipped_slot = NULL, equipped_at = NULL
FROM public.products p
WHERE ui.product_id = p.id AND p.public_id = 'theme_workout' AND ui.equipped_slot = 'theme';

--    2b. theme 슬롯이 빈 유저(방금 해제된 이들 포함)에게 theme_default 장착.
--        전 유저가 theme_default 보유(bootstrap 지급) — NOT EXISTS로 이미 장착자는 스킵.
UPDATE public.user_items ui
SET equipped_slot = 'theme', equipped_at = now()
FROM public.products p
WHERE ui.product_id = p.id AND p.public_id = 'theme_default'
  AND NOT EXISTS (
    SELECT 1 FROM public.user_items e WHERE e.user_id = ui.user_id AND e.equipped_slot = 'theme'
  );

-- 3) theme_workout 비활성화 → 상점·인벤토리·장착 API(is_active 필터)에서 자동 미노출.
UPDATE public.products SET is_active = false WHERE public_id = 'theme_workout';

COMMIT;
