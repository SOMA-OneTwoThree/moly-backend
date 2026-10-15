-- Accounts created at or after app_config.signup_trial_until get no 48-hour signup trial.
-- Without that key nothing changes, so this can be applied before the app release; the function
-- bodies change the preflight catalog, so apply it right before deploying the matching backend.
-- Requires bootstrap_no_default_routines.sql: if the target bootstrap_user still inserts
-- public.routines, apply that file first, or this one silently drops the default routines.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';

CREATE OR REPLACE FUNCTION public.bootstrap_user(p_user_id uuid, p_created_at timestamp with time zone DEFAULT now()) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
DECLARE
  v_required_count integer;
  v_signup_trial_until timestamptz;
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

  SELECT (value #>> '{}')::timestamptz INTO v_signup_trial_until
  FROM public.app_config WHERE key = 'signup_trial_until';

  INSERT INTO public.profiles (id, trial_ends_at)
  VALUES (p_user_id, CASE WHEN v_signup_trial_until IS NULL OR p_created_at < v_signup_trial_until
    THEN p_created_at + interval '48 hours' END)
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

CREATE OR REPLACE FUNCTION public.subscription_launch_access(p_user_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
  v_config jsonb;
  v_launch_until timestamptz;
  v_signup_trial_until timestamptz;
  v_profile public.profiles%ROWTYPE;
  v_created timestamptz;
  v_now timestamptz := statement_timestamp();
  v_new_member boolean;
  v_open boolean;
BEGIN
  SELECT value INTO v_config FROM public.app_config WHERE key = 'subscription_launch';
  SELECT (value #>> '{}')::timestamptz INTO v_launch_until FROM public.app_config WHERE key = 'free_launch_until';
  SELECT (value #>> '{}')::timestamptz INTO v_signup_trial_until FROM public.app_config WHERE key = 'signup_trial_until';
  SELECT * INTO v_profile FROM public.profiles WHERE id = p_user_id;
  SELECT created_at INTO v_created FROM auth.users WHERE id = p_user_id;
  v_new_member := v_profile.app_trial_started_at IS NOT NULL OR COALESCE(v_created >= CASE
    WHEN v_now < v_launch_until THEN (v_config->>'new_member_since')::timestamptz
    ELSE v_launch_until END, false);
  v_open := v_profile.nickname IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM public.subscriptions WHERE user_id = p_user_id
      AND status IN ('active', 'grace_period') AND expires_at > v_now);
  RETURN jsonb_build_object(
    'enabled', true,
    'self_trial_available', COALESCE(v_open AND v_new_member
      AND v_profile.app_trial_started_at IS NULL AND v_now < v_created + interval '48 hours'
      AND (v_signup_trial_until IS NULL OR v_created < v_signup_trial_until), false),
    'legacy_offer_eligible', COALESCE(v_open AND NOT v_new_member
      AND v_now < (v_config->>'legacy_offer_expires_at')::timestamptz, false));
END;
$$;
COMMIT;
