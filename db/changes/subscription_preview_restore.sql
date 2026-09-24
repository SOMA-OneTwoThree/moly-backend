-- Restore only explicitly listed pre-release accounts; no config/account/code mutations.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM (VALUES
      ('public.subscription_launch_config(uuid)','5d75529885b1a96419b566f5ac0ba227'),
      ('public.subscription_launch_access(uuid)','54df4e97e540e21654518123cef06ac3'),
      ('public.start_subscription_trial(uuid)','7e1a7dc6a37f7bd9641fc8c6a4853190'),
      ('public.subscription_offer_status(uuid)','edd6bbca89211c16aadf00ba58522a69'),
      ('public.claim_subscription_offer(uuid,text,text)','ae3c90ec81116e5451fdc8a422f8f55c')
    ) expected(signature, before_md5)
    WHERE COALESCE(md5(pg_get_functiondef(to_regprocedure(signature))), '') <> before_md5)
  THEN RAISE EXCEPTION 'subscription preview baseline differs; no changes applied'; END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.subscription_launch_config(p_user_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v_live jsonb; v_test jsonb; v_mode text; v_expires timestamptz;
BEGIN
  SELECT value INTO v_live FROM public.app_config WHERE key = 'subscription_launch' FOR SHARE;
  IF jsonb_typeof(v_live) IS DISTINCT FROM 'object' THEN RETURN '{}'::jsonb; END IF;
  v_live := v_live - '_test_mode';
  IF (v_live->'enabled') IS DISTINCT FROM 'false'::jsonb THEN RETURN v_live; END IF;
  SELECT value INTO v_test FROM public.app_config WHERE key = 'subscription_launch_test' FOR SHARE;
  IF jsonb_typeof(v_test) IS DISTINCT FROM 'object' THEN RETURN v_live; END IF;
  v_mode := v_test #>> ARRAY['accounts', p_user_id::text];
  IF v_mode IS NULL OR v_mode NOT IN ('regular', 'legacy_offer') THEN RETURN v_live; END IF;
  IF v_mode = 'legacy_offer' AND (jsonb_typeof(v_test->'campaign_id') IS DISTINCT FROM 'string'
    OR NULLIF(v_test->>'campaign_id', '') IS NULL
    OR v_test->'campaign_id' = v_live->'campaign_id') THEN RETURN v_live; END IF;
  IF COALESCE(v_test->>'expires_at', '') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]+)?Z$' THEN RETURN v_live; END IF;
  BEGIN v_expires := (v_test->>'expires_at')::timestamptz;
  EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN RETURN v_live; END;
  IF v_expires IS NULL OR NOT isfinite(v_expires) OR v_expires <= statement_timestamp()
    OR to_char(v_expires AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS') <> left(v_test->>'expires_at', 19)
  THEN RETURN v_live; END IF;
  RETURN (v_test - 'accounts' - 'existing_user_cutoff') || jsonb_build_object(
    'enabled', true, '_test_mode', v_mode, 'legacy_offer_expires_at', v_test->>'expires_at');
END;
$$;

CREATE OR REPLACE FUNCTION public.subscription_launch_access(p_user_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
  v_config jsonb := public.subscription_launch_config(p_user_id);
  v_cutoff timestamptz; v_expiry timestamptz; v_created timestamptz;
  v_enabled boolean := false; v_legacy boolean := false;
BEGIN
  IF v_config->>'_test_mode' IN ('regular', 'legacy_offer') THEN
    RETURN jsonb_build_object('enabled', true, 'legacy_offer_eligible', v_config->>'_test_mode' = 'legacy_offer');
  END IF;
  IF v_config->'enabled' = 'true'::jsonb
    AND COALESCE(v_config->>'existing_user_cutoff', '') ~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN
    BEGIN v_cutoff := (v_config->>'existing_user_cutoff')::timestamptz;
    EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN v_cutoff := NULL; END;
    v_enabled := v_cutoff IS NOT NULL AND isfinite(v_cutoff) AND statement_timestamp() >= v_cutoff;
    BEGIN v_expiry := (v_config->>'legacy_offer_expires_at')::timestamptz;
    EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN v_expiry := NULL; END;
    SELECT created_at INTO v_created FROM auth.users WHERE id = p_user_id;
    v_legacy := v_enabled AND v_created < v_cutoff AND v_expiry IS NOT NULL
      AND isfinite(v_expiry) AND v_expiry > statement_timestamp() AND v_expiry > v_cutoff;
  END IF;
  RETURN jsonb_build_object('enabled', v_enabled, 'legacy_offer_eligible', COALESCE(v_legacy, false));
END;
$$;
REVOKE ALL ON FUNCTION public.subscription_launch_access(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.subscription_launch_access(uuid) TO service_role;

CREATE OR REPLACE FUNCTION public.start_subscription_trial(
  p_user_id uuid
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
  v_profile public.profiles%ROWTYPE;
  v_config jsonb;
  v_cutoff timestamptz;
  v_created_at timestamptz;
  v_now timestamptz := statement_timestamp();
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  SELECT * INTO v_profile FROM public.profiles WHERE id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'profile not found'; END IF;
  -- Retry always preserves the original interval, even after expiry or deactivation.
  IF v_profile.app_trial_started_at IS NOT NULL THEN RETURN; END IF;
  v_config := public.subscription_launch_config(p_user_id);
  IF v_config->>'_test_mode' IN ('regular', 'legacy_offer') THEN
    v_created_at := v_now;
    IF v_profile.nickname IS NULL THEN RAISE EXCEPTION 'trial unavailable'; END IF;
  ELSE
  BEGIN
    v_cutoff := (v_config->>'existing_user_cutoff')::timestamptz;
  EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
    RAISE EXCEPTION 'trial unavailable';
  END;
  SELECT created_at INTO v_created_at FROM auth.users WHERE id = p_user_id;
  IF v_cutoff IS NULL OR NOT isfinite(v_cutoff) OR v_now < v_cutoff
    OR COALESCE(v_config->>'existing_user_cutoff', '') !~ '(Z|[+-][0-9]{2}:[0-9]{2})$'
    OR v_created_at IS NULL OR v_created_at < v_cutoff OR v_created_at > v_now
    OR v_now >= v_created_at + interval '48 hours'
    OR (v_config->'enabled') IS DISTINCT FROM 'true'::jsonb
    OR v_profile.nickname IS NULL THEN
    RAISE EXCEPTION 'trial unavailable';
  END IF;
  END IF;
  IF EXISTS (
    SELECT 1 FROM public.subscriptions
    WHERE user_id = p_user_id AND status IN ('active', 'grace_period')
      AND expires_at > v_now
  ) THEN RETURN; END IF;
  UPDATE public.profiles
  SET app_trial_started_at = v_created_at, app_trial_ends_at = v_created_at + interval '48 hours'
  WHERE id = p_user_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.subscription_offer_status(p_user_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
  v_config jsonb;
  v_claim public.subscription_offer_claims%ROWTYPE;
  v_platform text;
  v_plan text;
  v_offer jsonb;
  v_ready boolean;
  v_result jsonb := '{"legacy_offer_eligible":false,"ios_offer_ready":false,"android_offer_ready":false,"claimed_offer":null,"offer_redeemed":false}'::jsonb;
BEGIN
  v_config := public.subscription_launch_config(p_user_id);
  SELECT * INTO v_claim FROM public.subscription_offer_claims WHERE user_id = p_user_id;
  IF FOUND THEN
    v_result := jsonb_set(v_result, '{claimed_offer}', jsonb_build_object('platform', v_claim.platform, 'plan', v_claim.plan));
    v_result := jsonb_set(v_result, '{offer_redeemed}', to_jsonb(v_claim.redeemed_at IS NOT NULL));
  END IF;
  IF (v_config->'enabled') IS DISTINCT FROM 'true'::jsonb
    OR NULLIF(v_config->>'campaign_id', '') IS NULL OR v_claim.redeemed_at IS NOT NULL
    OR (v_claim.user_id IS NOT NULL AND v_claim.campaign_id <> v_config->>'campaign_id') THEN RETURN v_result; END IF;
  IF (public.subscription_launch_access(p_user_id)->'legacy_offer_eligible') IS DISTINCT FROM 'true'::jsonb
    OR NOT EXISTS (SELECT 1 FROM public.profiles WHERE id = p_user_id AND nickname IS NOT NULL)
    OR EXISTS (SELECT 1 FROM public.subscriptions WHERE user_id = p_user_id
      AND status IN ('active', 'grace_period') AND expires_at > statement_timestamp())
    THEN RETURN v_result; END IF;
  v_result := jsonb_set(v_result, '{legacy_offer_eligible}', 'true'::jsonb);
  FOREACH v_platform IN ARRAY ARRAY['ios', 'android'] LOOP
    v_ready := true;
    FOREACH v_plan IN ARRAY ARRAY['monthly', 'yearly'] LOOP
      -- An allocated iOS code fixes its plan; retries do not need unclaimed inventory.
      IF v_claim.user_id IS NOT NULL AND (v_platform <> v_claim.platform OR (v_platform = 'ios' AND v_plan <> v_claim.plan)) THEN CONTINUE; END IF;
      v_offer := v_config #> ARRAY['offers', v_platform, v_plan];
      IF (v_offer->'ready') IS DISTINCT FROM 'true'::jsonb OR NULLIF(v_offer->>'product_id', '') IS NULL THEN
        v_ready := false;
      ELSIF v_platform = 'ios' THEN
        IF NULLIF(v_offer->>'offer_id', '') IS NULL
          OR COALESCE(v_config->>'apple_app_id', '') !~ '^[0-9]+$' OR NOT EXISTS (
          SELECT 1 FROM public.subscription_trial_codes
          WHERE campaign_id = v_config->>'campaign_id' AND plan = v_plan
            AND expires_at > statement_timestamp()
            AND ((v_claim.user_id IS NULL AND claimed_at IS NULL)
              OR (id = v_claim.code_id AND claimed_by = p_user_id))
        ) THEN v_ready := false; END IF;
      ELSIF NULLIF(v_offer->>'base_plan_id', '') IS NULL OR NULLIF(v_offer->>'offer_id', '') IS NULL THEN
        v_ready := false;
      END IF;
    END LOOP;
    IF v_claim.user_id IS NOT NULL AND v_platform <> v_claim.platform THEN v_ready := false; END IF;
    v_result := jsonb_set(v_result, ARRAY[v_platform || '_offer_ready'], to_jsonb(v_ready));
  END LOOP;
  RETURN v_result;
END;
$$;

CREATE OR REPLACE FUNCTION public.claim_subscription_offer(
  p_user_id uuid, p_platform text, p_plan text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
  v_config jsonb;
  v_offer jsonb;
  v_claim public.subscription_offer_claims%ROWTYPE;
  v_code public.subscription_trial_codes%ROWTYPE;
  v_status jsonb;
BEGIN
  IF p_platform IS NULL OR p_plan IS NULL OR p_platform NOT IN ('ios', 'android') OR p_plan NOT IN ('monthly', 'yearly') THEN RAISE EXCEPTION 'invalid offer selection'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  PERFORM 1 FROM public.profiles WHERE id = p_user_id AND nickname IS NOT NULL FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'offer unavailable'; END IF;
  v_config := public.subscription_launch_config(p_user_id);
  IF (v_config->'enabled') IS DISTINCT FROM 'true'::jsonb THEN RAISE EXCEPTION 'offer unavailable'; END IF;
  IF (public.subscription_launch_access(p_user_id)->'legacy_offer_eligible') IS DISTINCT FROM 'true'::jsonb THEN RAISE EXCEPTION 'offer unavailable'; END IF;
  IF EXISTS (SELECT 1 FROM public.subscriptions WHERE user_id = p_user_id
    AND status IN ('active', 'grace_period') AND expires_at > statement_timestamp()) THEN RAISE EXCEPTION 'already subscribed'; END IF;
  SELECT * INTO v_claim FROM public.subscription_offer_claims WHERE user_id = p_user_id;
  IF FOUND AND (v_claim.platform <> p_platform OR (p_platform = 'ios' AND v_claim.plan <> p_plan)
    OR v_claim.campaign_id <> v_config->>'campaign_id' OR v_claim.redeemed_at IS NOT NULL) THEN RAISE EXCEPTION 'offer already selected'; END IF;
  v_status := public.subscription_offer_status(p_user_id);
  IF (v_status->(p_platform || '_offer_ready')) IS DISTINCT FROM 'true'::jsonb THEN RAISE EXCEPTION 'offer unavailable'; END IF;
  v_offer := v_config #> ARRAY['offers', p_platform, p_plan];
  IF v_claim.user_id IS NOT NULL AND p_platform = 'android' THEN
    UPDATE public.subscription_offer_claims SET plan = p_plan, product_id = v_offer->>'product_id',
      base_plan_id = v_offer->>'base_plan_id', offer_id = v_offer->>'offer_id'
    WHERE user_id = p_user_id RETURNING * INTO v_claim;
  END IF;
  IF v_claim.user_id IS NULL THEN
    IF p_platform = 'ios' THEN
      SELECT * INTO v_code FROM public.subscription_trial_codes
      WHERE campaign_id = v_config->>'campaign_id' AND plan = p_plan
        AND claimed_at IS NULL AND expires_at > statement_timestamp()
      ORDER BY expires_at, id LIMIT 1 FOR UPDATE SKIP LOCKED;
      IF NOT FOUND THEN RAISE EXCEPTION 'offer unavailable'; END IF;
      UPDATE public.subscription_trial_codes SET claimed_by = p_user_id, claimed_at = statement_timestamp() WHERE id = v_code.id;
    END IF;
    INSERT INTO public.subscription_offer_claims(user_id, campaign_id, platform, plan, code_id, product_id, base_plan_id, offer_id)
    VALUES(p_user_id, v_config->>'campaign_id', p_platform, p_plan, v_code.id,
      v_offer->>'product_id', v_offer->>'base_plan_id', v_offer->>'offer_id') RETURNING * INTO v_claim;
  END IF;
  IF p_platform = 'ios' THEN
    SELECT * INTO v_code FROM public.subscription_trial_codes WHERE id = v_claim.code_id;
    IF v_code.expires_at <= statement_timestamp() THEN RAISE EXCEPTION 'offer expired'; END IF;
    RETURN jsonb_build_object('platform', p_platform, 'plan', p_plan,
      'apple_app_id', v_config->>'apple_app_id', 'code', v_code.code);
  END IF;
  RETURN jsonb_build_object('platform', p_platform, 'plan', p_plan, 'product_id', v_claim.product_id,
    'base_plan_id', v_claim.base_plan_id, 'offer_id', v_claim.offer_id);
END;
$$;


REVOKE ALL ON FUNCTION public.subscription_launch_config(uuid) FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.subscription_launch_config(uuid) TO service_role;

REVOKE ALL ON FUNCTION public.subscription_launch_access(uuid) FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.subscription_launch_access(uuid) TO service_role;

REVOKE ALL ON FUNCTION public.start_subscription_trial(uuid) FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.start_subscription_trial(uuid) TO service_role;

REVOKE ALL ON FUNCTION public.subscription_offer_status(uuid) FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.subscription_offer_status(uuid) TO service_role;

REVOKE ALL ON FUNCTION public.claim_subscription_offer(uuid, text, text) FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.claim_subscription_offer(uuid, text, text) TO service_role;

COMMIT;
