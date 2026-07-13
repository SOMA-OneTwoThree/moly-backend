-- Appearance v2 expand migration.
-- 기존 서버가 동작하는 동안 먼저 적용할 수 있는 additive 단계다.
BEGIN;

ALTER TABLE public.products ADD COLUMN IF NOT EXISTS public_id text;
ALTER TABLE public.products ADD COLUMN IF NOT EXISTS asset_version integer;

UPDATE public.products
SET public_id = CASE id
  WHEN '00000000-0000-4000-8000-000000000101' THEN 'theme_default'
  WHEN '00000000-0000-4000-8000-000000000102' THEN 'theme_workout'
  WHEN '00000000-0000-4000-8000-000000000201' THEN 'head_sunglasses'
  WHEN '00000000-0000-4000-8000-000000000202' THEN 'head_mandarin'
  WHEN '00000000-0000-4000-8000-000000000301' THEN 'neck_employee_badge'
  WHEN '00000000-0000-4000-8000-000000000302' THEN 'neck_muffler'
END
WHERE id IN (
  '00000000-0000-4000-8000-000000000101',
  '00000000-0000-4000-8000-000000000102',
  '00000000-0000-4000-8000-000000000201',
  '00000000-0000-4000-8000-000000000202',
  '00000000-0000-4000-8000-000000000301',
  '00000000-0000-4000-8000-000000000302'
);

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM public.products
    WHERE product_type = 'cosmetic' AND public_id IS NULL
  ) THEN
    RAISE EXCEPTION 'assign public_id to every cosmetic before appearance v2 expand';
  END IF;
END;
$$;

CREATE UNIQUE INDEX IF NOT EXISTS products_public_id_uq
  ON public.products (public_id) WHERE public_id IS NOT NULL;

-- expand 단계에서는 background와 theme을 함께 허용한다. cutover에서 background를 제거한다.
ALTER TABLE public.products DROP CONSTRAINT IF EXISTS products_slot_check;
ALTER TABLE public.products ADD CONSTRAINT products_slot_check
  CHECK (slot IN ('background','theme','head','neck','body'));

ALTER TABLE public.products DROP CONSTRAINT IF EXISTS products_cosmetic_ck;
ALTER TABLE public.products ADD CONSTRAINT products_cosmetic_ck CHECK (
  product_type <> 'cosmetic' OR (
    public_id IS NOT NULL AND slot IS NOT NULL AND assets IS NOT NULL
    AND hay_amount IS NULL AND app_store_product_id IS NULL AND price_krw IS NULL
  )
);

COMMIT;
