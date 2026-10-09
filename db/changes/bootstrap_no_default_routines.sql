-- Routine template release: new accounts start without the two default routines.
-- Apply right before deploying the release PR's backend image, whose schema contract has this
-- bootstrap_user body. The routine-redesign image compares function bodies in its preflight, so it
-- deploys again only after the previous body is restored (see Rollback).
-- Signup keeps running handle_new_user -> bootstrap_user in the auth.users insert transaction;
-- profiles, gifts and the deletion barrier are unchanged.
-- Also gives the default routines that bootstrap_user created after routine_redesign.sql (seedling/peach,
-- no template_id) their template icon, color and template_id. Like the 2026-10-08 backfill, only rows
-- still at the untouched seedling/peach are changed (icon and color are user-editable). Their missing
-- weekday history stays a code fallback (created_at in the profile time zone, current days).
-- Expected rows, read-only before applying:
--   SELECT count(*) FROM public.routines
--   WHERE template_id IS NULL AND icon = 'seedling' AND color = 'peach'
--     AND name_i18n ->> 'ko' IN ('이불 정리하기', '물 마시기');
-- Rollback: re-create bootstrap_user from the routine-redesign schema.sql (CREATE OR REPLACE FUNCTION
-- with the default routine INSERT), then deploy that image. The icon backfill can stay.
-- BEGIN/COMMIT keep SET LOCAL effective under plain psql; db.apply strips them and uses its own transaction.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';

CREATE OR REPLACE FUNCTION public.bootstrap_user(p_user_id uuid, p_created_at timestamp with time zone DEFAULT now()) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
DECLARE
  v_required_count integer;
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
END;
$$;

UPDATE public.routines SET icon = 'bed', color = 'peach', template_id = 'make_bed'
WHERE name_i18n ->> 'ko' = '이불 정리하기' AND template_id IS NULL AND icon = 'seedling' AND color = 'peach';
UPDATE public.routines SET icon = 'droplet', color = 'blue', template_id = 'drink_water'
WHERE name_i18n ->> 'ko' = '물 마시기' AND template_id IS NULL AND icon = 'seedling' AND color = 'peach';
COMMIT;
-- Validate with the release schema contract (uv run python -m db.verify --env <env>).
