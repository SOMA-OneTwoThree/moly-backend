-- 루틴 "주 N회" 모드 제거 — 요일별(days_of_week)만 지원.
-- 서버 배포 전에 먼저 적용한다(새 코드는 days_of_week가 non-null임을 전제).
-- 1) days_of_week NULL 행(부트스트랩 기본 루틴, freq=7)을 매일로 백필
-- 2) days_of_week NOT NULL 제약
-- 3) bootstrap_user 재정의: 기본 루틴 삽입에 days_of_week 포함

BEGIN;

UPDATE public.routines
SET days_of_week = '{1,2,3,4,5,6,7}', frequency_per_week = 7
WHERE days_of_week IS NULL;

ALTER TABLE public.routines ALTER COLUMN days_of_week SET NOT NULL;

-- 신규 가입 트리거와 moly-auth self-heal이 공유하는 원자적 부트스트랩.
-- (20260713_appearance_v2_cutover.sql 정의에서 루틴 INSERT에 days_of_week만 추가)
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
    INSERT INTO public.routines (user_id, name, frequency_per_week, days_of_week, reminder_enabled)
    VALUES (p_user_id, '이불 정리하기', 7, '{1,2,3,4,5,6,7}', false),
           (p_user_id, '물 마시기', 7, '{1,2,3,4,5,6,7}', false);
  END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.bootstrap_user(uuid, timestamptz) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.bootstrap_user(uuid, timestamptz) TO service_role;

COMMIT;
