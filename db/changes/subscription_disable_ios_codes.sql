-- Disable new iOS code enrollment. Preserve issued codes, claims and all financial data.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM (VALUES
    ('public.subscription_offer_status(uuid)','d92425f07f68fd699bd0085323c7afa6'),
    ('public.claim_subscription_offer(uuid,text,text)','27139af1b18aca63b87a287331d783c5')
  ) expected(signature,before_md5)
  WHERE COALESCE(md5(pg_get_functiondef(to_regprocedure(signature))),'') <> before_md5)
  THEN RAISE EXCEPTION 'subscription offer baseline differs; no changes applied'; END IF;
END; $$;

CREATE OR REPLACE FUNCTION public.subscription_offer_status(p_user_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
  v_config jsonb := public.subscription_launch_config(p_user_id);
  v_claim public.subscription_offer_claims%ROWTYPE;
  v_plan text;
  v_offer jsonb;
  v_ready boolean := true;
  v_result jsonb := '{"legacy_offer_eligible":false,"ios_offer_ready":false,"android_offer_ready":false,"claimed_offer":null,"offer_redeemed":false}'::jsonb;
BEGIN
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
  v_config := public.subscription_launch_config(p_user_id);
  IF (v_config->'enabled') IS DISTINCT FROM 'true'::jsonb
    OR (public.subscription_launch_access(p_user_id)->'legacy_offer_eligible') IS DISTINCT FROM 'true'::jsonb
    THEN RAISE EXCEPTION 'offer unavailable'; END IF;
  IF EXISTS (SELECT 1 FROM public.subscriptions WHERE user_id = p_user_id
    AND status IN ('active', 'grace_period') AND expires_at > statement_timestamp()) THEN RAISE EXCEPTION 'already subscribed'; END IF;
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

REVOKE ALL ON FUNCTION public.subscription_offer_status(uuid) FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.subscription_offer_status(uuid) TO service_role;
REVOKE ALL ON FUNCTION public.claim_subscription_offer(uuid,text,text) FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.claim_subscription_offer(uuid,text,text) TO service_role;

COMMIT;
