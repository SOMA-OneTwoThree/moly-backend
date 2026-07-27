-- 적용: python db/apply.py db/migrations/20260727_catalog_i18n.sql --commit
-- DB 카탈로그 다국어(SOMA-346): products·routines에 name_i18n(jsonb) 추가.
--   · 기존 cosmetic 상품은 public_id(안정 키) 기준으로 번역 백필. 원문 name(ko)은 보존.
--   · 기본 루틴은 provenance(is_default/source) 컬럼이 없어 이름 매칭 백필이 유저가 만든 동명
--     루틴을 오염시킬 수 있다 → 기존 루틴은 백필하지 않고, 신규 가입자만 bootstrap_user가
--     name_i18n와 함께 생성한다(과거 마이그레이션은 재실행되지 않으므로 여기서 CREATE OR REPLACE).
--   · 렌더는 resolve(lang)→en→ko→원문 순 폴백이라 name_i18n=NULL/부분키도 안전.
BEGIN;

ALTER TABLE public.products ADD COLUMN IF NOT EXISTS name_i18n jsonb;
ALTER TABLE public.routines ADD COLUMN IF NOT EXISTS name_i18n jsonb;

-- name_i18n은 object만 허용 — scalar(문자열·숫자)가 들어오면 렌더 시 .get 500(코드 isinstance와 이중 방어).
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'products_name_i18n_obj_ck') THEN
    ALTER TABLE public.products ADD CONSTRAINT products_name_i18n_obj_ck
      CHECK (name_i18n IS NULL OR jsonb_typeof(name_i18n) = 'object');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'routines_name_i18n_obj_ck') THEN
    ALTER TABLE public.routines ADD CONSTRAINT routines_name_i18n_obj_ck
      CHECK (name_i18n IS NULL OR jsonb_typeof(name_i18n) = 'object');
  END IF;
END $$;

-- cosmetic 상품 번역(일본어는 한국어 기준 번역, 고유명사 유지). 원문 name 미변경.
UPDATE public.products AS p SET name_i18n = m.i18n
FROM (VALUES
  ('theme_default',         '{"ko":"집","en":"Home","ja":"おうち"}'::jsonb),
  ('theme_workout',         '{"ko":"운동","en":"Workout","ja":"運動"}'::jsonb),
  ('head_sunglasses',       '{"ko":"선글라스","en":"Sunglasses","ja":"サングラス"}'::jsonb),
  ('head_mandarin',         '{"ko":"머리 위에 귤","en":"Tangerine on Head","ja":"頭の上のみかん"}'::jsonb),
  ('neck_employee_badge',   '{"ko":"사원증","en":"ID Badge","ja":"社員証"}'::jsonb),
  ('neck_muffler',          '{"ko":"목도리","en":"Scarf","ja":"マフラー"}'::jsonb),
  ('head_suncream',         '{"ko":"선크림","en":"Sunscreen","ja":"日焼け止め"}'::jsonb),
  ('head_glasses',          '{"ko":"동글이 안경","en":"Round Glasses","ja":"丸メガネ"}'::jsonb),
  ('neck_shell',            '{"ko":"조개 목걸이","en":"Shell Necklace","ja":"貝のネックレス"}'::jsonb),
  ('clothes_hawaiianpants', '{"ko":"하와이안 바지","en":"Hawaiian Pants","ja":"アロハパンツ"}'::jsonb)
) AS m(public_id, i18n)
WHERE p.public_id = m.public_id;

-- bootstrap_user 갱신 — 신규 가입자 기본 루틴을 name_i18n와 함께 생성.
-- (본문은 20260719_hat_glasses_rightside.sql의 현행 정의 + 루틴 INSERT에 name_i18n만 추가.)
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

COMMIT;
