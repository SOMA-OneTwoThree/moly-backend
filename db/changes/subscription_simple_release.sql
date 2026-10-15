-- Replace the subscription enabled flag and tester allowlist with one release clock,
-- free_launch_until. Old servers keep launch access while free_launch_until is in the future,
-- so this can be applied before the matching backend/auth deploy.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';

CREATE OR REPLACE FUNCTION public.subscription_launch_access(p_user_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
  v_config jsonb;
  v_launch_until timestamptz;
  v_profile public.profiles%ROWTYPE;
  v_created timestamptz;
  v_now timestamptz := statement_timestamp();
  v_new_member boolean;
  v_open boolean;
BEGIN
  SELECT value INTO v_config FROM public.app_config WHERE key = 'subscription_launch';
  SELECT (value #>> '{}')::timestamptz INTO v_launch_until FROM public.app_config WHERE key = 'free_launch_until';
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
      AND v_profile.app_trial_started_at IS NULL AND v_now < v_created + interval '48 hours', false),
    'legacy_offer_eligible', COALESCE(v_open AND NOT v_new_member
      AND v_now < (v_config->>'legacy_offer_expires_at')::timestamptz, false));
END;
$$;

-- Retries keep the original signup interval, even after expiry.
CREATE OR REPLACE FUNCTION public.start_subscription_trial(
  p_user_id uuid
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
  v_profile public.profiles%ROWTYPE;
  v_created_at timestamptz;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  SELECT * INTO v_profile FROM public.profiles WHERE id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'profile not found'; END IF;
  IF v_profile.app_trial_started_at IS NOT NULL THEN RETURN; END IF;
  IF EXISTS (
    SELECT 1 FROM public.subscriptions
    WHERE user_id = p_user_id AND status IN ('active', 'grace_period')
      AND expires_at > statement_timestamp()
  ) THEN RETURN; END IF;
  IF (public.subscription_launch_access(p_user_id)->'self_trial_available') IS DISTINCT FROM 'true'::jsonb
    THEN RAISE EXCEPTION 'trial unavailable'; END IF;
  SELECT created_at INTO v_created_at FROM auth.users WHERE id = p_user_id;
  UPDATE public.profiles
  SET app_trial_started_at = v_created_at, app_trial_ends_at = v_created_at + interval '48 hours'
  WHERE id = p_user_id;
END;
$$;

-- Active offer enrollment is Android-only; historical iOS code records remain archived.
CREATE OR REPLACE FUNCTION public.subscription_offer_status(p_user_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
  v_config jsonb;
  v_claim public.subscription_offer_claims%ROWTYPE;
  v_plan text;
  v_offer jsonb;
  v_ready boolean := true;
  v_result jsonb := '{"legacy_offer_eligible":false,"ios_offer_ready":false,"android_offer_ready":false,"claimed_offer":null,"offer_redeemed":false}'::jsonb;
BEGIN
  SELECT value INTO v_config FROM public.app_config WHERE key = 'subscription_launch';
  SELECT * INTO v_claim FROM public.subscription_offer_claims WHERE user_id = p_user_id;
  IF FOUND THEN
    v_result := jsonb_set(v_result, '{claimed_offer}', jsonb_build_object('platform', v_claim.platform, 'plan', v_claim.plan));
    v_result := jsonb_set(v_result, '{offer_redeemed}', to_jsonb(v_claim.redeemed_at IS NOT NULL));
  END IF;
  IF NULLIF(v_config->>'campaign_id', '') IS NULL OR v_claim.redeemed_at IS NOT NULL
    OR (v_claim.user_id IS NOT NULL AND v_claim.campaign_id <> v_config->>'campaign_id') THEN RETURN v_result; END IF;
  IF (public.subscription_launch_access(p_user_id)->'legacy_offer_eligible') IS DISTINCT FROM 'true'::jsonb
    THEN RETURN v_result; END IF;
  -- Keep eligibility visible so an unavailable iOS campaign cannot become a paid fallback.
  v_result := jsonb_set(v_result, '{legacy_offer_eligible}', 'true'::jsonb);
  IF v_claim.user_id IS NOT NULL AND v_claim.platform <> 'android' THEN RETURN v_result; END IF;
  FOREACH v_plan IN ARRAY ARRAY['monthly', 'yearly'] LOOP
    v_offer := v_config #> ARRAY['offers', 'android', v_plan];
    IF (v_offer->'ready') IS DISTINCT FROM 'true'::jsonb
      OR NULLIF(v_offer->>'product_id', '') IS NULL
      OR NULLIF(v_offer->>'base_plan_id', '') IS NULL
      OR NULLIF(v_offer->>'offer_id', '') IS NULL THEN v_ready := false; END IF;
  END LOOP;
  RETURN jsonb_set(v_result, '{android_offer_ready}', to_jsonb(v_ready));
END;
$$;

CREATE OR REPLACE FUNCTION public.claim_subscription_offer(
  p_user_id uuid, p_platform text, p_plan text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
  v_config jsonb;
  v_offer jsonb;
  v_claim public.subscription_offer_claims%ROWTYPE;
BEGIN
  IF p_platform IS DISTINCT FROM 'android' THEN RAISE EXCEPTION 'offer unavailable'; END IF;
  IF p_plan IS NULL OR p_plan NOT IN ('monthly', 'yearly') THEN RAISE EXCEPTION 'invalid offer selection'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  PERFORM 1 FROM public.profiles WHERE id = p_user_id AND nickname IS NOT NULL FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'offer unavailable'; END IF;
  SELECT value INTO v_config FROM public.app_config WHERE key = 'subscription_launch';
  IF (public.subscription_launch_access(p_user_id)->'legacy_offer_eligible') IS DISTINCT FROM 'true'::jsonb
    THEN RAISE EXCEPTION 'offer unavailable'; END IF;
  SELECT * INTO v_claim FROM public.subscription_offer_claims WHERE user_id = p_user_id;
  IF FOUND AND (v_claim.platform <> p_platform OR v_claim.campaign_id <> v_config->>'campaign_id'
    OR v_claim.redeemed_at IS NOT NULL) THEN RAISE EXCEPTION 'offer already selected'; END IF;
  IF (public.subscription_offer_status(p_user_id)->'android_offer_ready') IS DISTINCT FROM 'true'::jsonb
    THEN RAISE EXCEPTION 'offer unavailable'; END IF;
  v_offer := v_config #> ARRAY['offers', 'android', p_plan];
  IF v_claim.user_id IS NOT NULL THEN
    UPDATE public.subscription_offer_claims SET plan = p_plan, product_id = v_offer->>'product_id',
      base_plan_id = v_offer->>'base_plan_id', offer_id = v_offer->>'offer_id'
    WHERE user_id = p_user_id RETURNING * INTO v_claim;
  ELSE
    INSERT INTO public.subscription_offer_claims(user_id, campaign_id, platform, plan, product_id, base_plan_id, offer_id)
    VALUES(p_user_id, v_config->>'campaign_id', 'android', p_plan,
      v_offer->>'product_id', v_offer->>'base_plan_id', v_offer->>'offer_id') RETURNING * INTO v_claim;
  END IF;
  RETURN jsonb_build_object('platform', 'android', 'plan', p_plan, 'product_id', v_claim.product_id,
    'base_plan_id', v_claim.base_plan_id, 'offer_id', v_claim.offer_id);
END;
$$;

DROP FUNCTION public.subscription_launch_config(uuid);

REVOKE ALL ON FUNCTION public.subscription_launch_access(uuid) FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.subscription_launch_access(uuid) TO service_role;
REVOKE ALL ON FUNCTION public.start_subscription_trial(uuid) FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.start_subscription_trial(uuid) TO service_role;
REVOKE ALL ON FUNCTION public.subscription_offer_status(uuid) FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.subscription_offer_status(uuid) TO service_role;
REVOKE ALL ON FUNCTION public.claim_subscription_offer(uuid,text,text) FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.claim_subscription_offer(uuid,text,text) TO service_role;

-- Move the live offer settings out of the tester config. new_member_since is the moment
-- testers' fresh accounts start counting as new members before release. The offer deadline
-- is a pre-release placeholder: set the real one together with free_launch_until at release.
UPDATE public.app_config
SET value = jsonb_build_object(
  'new_member_since', now(),
  'campaign_id', 'launch-2026',
  'legacy_offer_expires_at', '2026-12-31T23:59:59+09:00',
  'offers', jsonb_build_object('android', (
    SELECT value->'offers'->'android' FROM public.app_config WHERE key = 'subscription_launch_test')))
WHERE key = 'subscription_launch';
DELETE FROM public.app_config WHERE key = 'subscription_launch_test';
COMMIT;
