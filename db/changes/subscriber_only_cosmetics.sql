-- Makes the hot spring theme, the towel and the painter outfit subscriber-only.
-- Subscriber-only cosmetics have no HAY price; active subscribers equip them without buying.
-- Apply after the app server that understands products.is_subscriber_only is deployed.
-- Expected: constraint replaced, 3 rows updated. Fails if anyone already owns one of them.
-- Rollback: set is_subscriber_only = false and restore price_hay (theme_onsen 4000,
--   head_towel 1000, body_painter 2000), then restore products_cosmetic_ck with
--   (is_subscriber_only = false) in place of the OR clause.
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM public.user_items ui
    JOIN public.products p ON p.id = ui.product_id
    WHERE p.public_id IN ('theme_onsen', 'head_towel', 'body_painter')
      AND ui.source <> 'subscription'
  ) THEN
    RAISE EXCEPTION 'subscriber-only candidates already have owners';
  END IF;
END $$;

ALTER TABLE public.products DROP CONSTRAINT products_cosmetic_ck;
ALTER TABLE public.products ADD CONSTRAINT products_cosmetic_ck CHECK (((product_type <> 'cosmetic'::text) OR ((public_id IS NOT NULL) AND (slot IS NOT NULL) AND (hay_amount IS NULL) AND (app_store_product_id IS NULL) AND (price_krw IS NULL) AND (play_store_product_id IS NULL) AND ((is_subscriber_only = false) OR (price_hay IS NULL)) AND ((is_active = false) OR ((asset_version IS NOT NULL) AND (asset_version >= 1) AND (assets IS NOT NULL))))));

UPDATE public.products
SET is_subscriber_only = true, price_hay = NULL
WHERE public_id IN ('theme_onsen', 'head_towel', 'body_painter');
