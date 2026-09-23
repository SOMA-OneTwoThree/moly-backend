-- Apply after subscription_launch.sql. Does not alter configuration or self trials.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';

CREATE OR REPLACE FUNCTION public.subscription_offer_status(p_user_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
  v_config jsonb;
  v_cutoff timestamptz;
  v_offer_expires_at timestamptz;
  v_claim public.subscription_offer_claims%ROWTYPE;
  v_platform text;
  v_plan text;
  v_offer jsonb;
  v_ready boolean;
  v_result jsonb := '{"ios_offer_ready":false,"android_offer_ready":false,"claimed_offer":null,"offer_redeemed":false}'::jsonb;
BEGIN
  SELECT value INTO v_config FROM public.app_config WHERE key = 'subscription_launch';
  SELECT * INTO v_claim FROM public.subscription_offer_claims WHERE user_id = p_user_id;
  IF FOUND THEN
    v_result := jsonb_set(v_result, '{claimed_offer}', jsonb_build_object('platform', v_claim.platform, 'plan', v_claim.plan));
    v_result := jsonb_set(v_result, '{offer_redeemed}', to_jsonb(v_claim.redeemed_at IS NOT NULL));
  END IF;
  IF (v_config->'enabled') IS DISTINCT FROM 'true'::jsonb
    OR NULLIF(v_config->>'campaign_id', '') IS NULL OR v_claim.redeemed_at IS NOT NULL
    OR (v_claim.user_id IS NOT NULL AND v_claim.campaign_id <> v_config->>'campaign_id') THEN RETURN v_result; END IF;
  BEGIN
    v_cutoff := (v_config->>'existing_user_cutoff')::timestamptz;
    v_offer_expires_at := (v_config->>'legacy_offer_expires_at')::timestamptz;
  EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN RETURN v_result; END;
  IF v_cutoff IS NULL OR v_offer_expires_at IS NULL OR NOT isfinite(v_offer_expires_at)
    OR v_offer_expires_at <= statement_timestamp() OR v_offer_expires_at <= v_cutoff
    THEN RETURN v_result; END IF;
  FOREACH v_platform IN ARRAY ARRAY['ios', 'android'] LOOP
    v_ready := true;
    FOREACH v_plan IN ARRAY ARRAY['monthly', 'yearly'] LOOP
      -- An allocated iOS code fixes its plan; retries do not need unclaimed inventory.
      IF v_claim.user_id IS NOT NULL AND (v_platform <> v_claim.platform OR (v_platform = 'ios' AND v_plan <> v_claim.plan)) THEN CONTINUE; END IF;
      v_offer := v_config #> ARRAY['offers', v_platform, v_plan];
      IF (v_offer->'ready') IS DISTINCT FROM 'true'::jsonb OR NULLIF(v_offer->>'product_id', '') IS NULL THEN
        v_ready := false;
      ELSIF v_platform = 'ios' THEN
        IF COALESCE(v_config->>'apple_app_id', '') !~ '^[0-9]+$' OR NOT EXISTS (
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
  v_created_at timestamptz;
  v_cutoff timestamptz;
  v_offer_expires_at timestamptz;
  v_status jsonb;
BEGIN
  IF p_platform NOT IN ('ios', 'android') OR p_plan NOT IN ('monthly', 'yearly') THEN RAISE EXCEPTION 'invalid offer selection'; END IF;
  PERFORM 1 FROM public.profiles WHERE id = p_user_id AND nickname IS NOT NULL FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'offer unavailable'; END IF;
  SELECT value INTO v_config FROM public.app_config WHERE key = 'subscription_launch';
  IF (v_config->'enabled') IS DISTINCT FROM 'true'::jsonb THEN RAISE EXCEPTION 'offer unavailable'; END IF;
  BEGIN
    v_cutoff := (v_config->>'existing_user_cutoff')::timestamptz;
    v_offer_expires_at := (v_config->>'legacy_offer_expires_at')::timestamptz;
  EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
    RAISE EXCEPTION 'offer unavailable';
  END;
  SELECT created_at INTO v_created_at FROM auth.users WHERE id = p_user_id;
  IF v_cutoff IS NULL OR v_created_at IS NULL OR v_created_at >= v_cutoff
    OR v_offer_expires_at IS NULL OR NOT isfinite(v_offer_expires_at)
    OR v_offer_expires_at <= statement_timestamp() OR v_offer_expires_at <= v_cutoff
    THEN RAISE EXCEPTION 'offer unavailable'; END IF;
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
REVOKE ALL ON FUNCTION public.subscription_offer_status(uuid) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.claim_subscription_offer(uuid, text, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.subscription_offer_status(uuid) TO service_role;
GRANT EXECUTE ON FUNCTION public.claim_subscription_offer(uuid, text, text) TO service_role;

COMMIT;
