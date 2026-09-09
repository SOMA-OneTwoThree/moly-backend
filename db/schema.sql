-- moly-backend: current application schema (PostgreSQL 17 / Supabase).
-- Edit this file for structural changes. It creates an EMPTY application schema;
-- existing environments use reviewed incremental SQL, never a baseline replay.
-- Supabase owns auth.users and its roles; install them before this schema.
-- Public/vecs definitions preserve the production catalog, including policy NULLs.

BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
SET LOCAL check_function_bodies = false;
SELECT pg_catalog.set_config('search_path', '', true);

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname IN ('public', 'vecs') AND c.relkind IN ('r', 'p')
      AND NOT EXISTS (
        SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass
          AND d.objid=c.oid AND d.deptype='e'
      )
  ) THEN
    RAISE EXCEPTION 'schema.sql requires an empty application schema; use reviewed change SQL';
  END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS public;
CREATE SCHEMA IF NOT EXISTS vecs;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

-- FUNCTION: public.bootstrap_user(uuid, timestamp with time zone)
CREATE FUNCTION public.bootstrap_user(p_user_id uuid, p_created_at timestamp with time zone DEFAULT now()) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
DECLARE
  v_required_count integer;
  v_profile_created integer;
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
  GET DIAGNOSTICS v_profile_created = ROW_COUNT;

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

-- FUNCTION: public.create_privacy_barrier_for_profile()
CREATE FUNCTION public.create_privacy_barrier_for_profile() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
BEGIN
  INSERT INTO public.privacy_subject_barriers (user_id, state, epoch)
  VALUES (NEW.id, 'active', 0)
  ON CONFLICT (user_id) DO NOTHING;
  RETURN NEW;
END;
$$;

-- FUNCTION: public.delete_user_memories(uuid)
CREATE FUNCTION public.delete_user_memories(p_user_id uuid) RETURNS integer
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO ''
    AS $$
  WITH deleted AS (
    DELETE FROM vecs.moly_memories_v2
    WHERE metadata->>'user_id' = p_user_id::text
    RETURNING 1
  )
  SELECT count(*)::integer FROM deleted;
$$;

-- FUNCTION: public.guard_normalized_memory_snapshot()
CREATE FUNCTION public.guard_normalized_memory_snapshot() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  IF TG_OP='INSERT' AND NEW.memory_mode='normalized' THEN
    NEW.memory_text := NULL;
    NEW.memory_refreshed_at := NULL;
  ELSIF TG_OP='UPDATE' AND OLD.memory_mode='normalized' THEN
    NEW.memory_mode := 'normalized';   -- downgrade 차단
    NEW.memory_text := NULL;
    NEW.memory_refreshed_at := NULL;
  END IF;
  RETURN NEW;
END $$;

-- FUNCTION: public.handle_new_user()
CREATE FUNCTION public.handle_new_user() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
BEGIN
  PERFORM public.bootstrap_user(NEW.id, NEW.created_at);
  RETURN NEW;
END;
$$;

-- FUNCTION: public.normalize_content_language(text)
CREATE FUNCTION public.normalize_content_language(tag text) RETURNS text
    LANGUAGE sql IMMUTABLE
    SET search_path TO 'public'
    AS $$
  SELECT CASE
    WHEN tag IS NULL OR btrim(tag) = '' THEN 'en'
    WHEN lower(split_part(btrim(tag), '-', 1)) IN ('ko', 'en', 'ja')
      THEN lower(split_part(btrim(tag), '-', 1))
    ELSE 'en'
  END
$$;

-- FUNCTION: public.normalize_profile_language()
CREATE FUNCTION public.normalize_profile_language() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
BEGIN
  NEW.language := public.normalize_content_language(NEW.language);
  RETURN NEW;
END;
$$;

-- FUNCTION: public.set_updated_at()
CREATE FUNCTION public.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

-- TABLE: public.ai_price_catalog
CREATE TABLE public.ai_price_catalog (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    catalog_version integer NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    input_micro_usd bigint,
    cached_input_micro_usd bigint,
    cache_write_micro_usd bigint,
    output_micro_usd bigint,
    embedding_micro_usd bigint,
    source_note text,
    effective_from timestamp with time zone NOT NULL,
    effective_to timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ai_price_catalog_cache_write_micro_usd_check CHECK ((cache_write_micro_usd >= 0)),
    CONSTRAINT ai_price_catalog_cached_input_micro_usd_check CHECK ((cached_input_micro_usd >= 0)),
    CONSTRAINT ai_price_catalog_check CHECK (((effective_to IS NULL) OR (effective_to > effective_from))),
    CONSTRAINT ai_price_catalog_embedding_micro_usd_check CHECK ((embedding_micro_usd >= 0)),
    CONSTRAINT ai_price_catalog_input_micro_usd_check CHECK ((input_micro_usd >= 0)),
    CONSTRAINT ai_price_catalog_output_micro_usd_check CHECK ((output_micro_usd >= 0))
);

-- TABLE: public.ai_usage_daily_rollup
CREATE TABLE public.ai_usage_daily_rollup (
    kst_date date NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    lane text NOT NULL,
    purpose text NOT NULL,
    status text NOT NULL,
    calls bigint DEFAULT 0 NOT NULL,
    input_tokens bigint DEFAULT 0 NOT NULL,
    cached_input_tokens bigint DEFAULT 0 NOT NULL,
    cache_write_tokens bigint DEFAULT 0 NOT NULL,
    output_tokens bigint DEFAULT 0 NOT NULL,
    embedding_tokens bigint DEFAULT 0 NOT NULL,
    cost_micro_usd bigint DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

-- TABLE: public.ai_usage_ledger
CREATE TABLE public.ai_usage_ledger (
    call_id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid,
    turn_seq bigint,
    job_id uuid,
    activity_date date,
    lane text NOT NULL,
    purpose text NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    model_snapshot text,
    status text DEFAULT 'started'::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    latency_ms integer,
    input_tokens integer DEFAULT 0 NOT NULL,
    cached_input_tokens integer DEFAULT 0 NOT NULL,
    cache_write_tokens integer DEFAULT 0 NOT NULL,
    output_tokens integer DEFAULT 0 NOT NULL,
    embedding_tokens integer DEFAULT 0 NOT NULL,
    cache_write_estimated boolean DEFAULT false NOT NULL,
    provider_request_id text,
    price_catalog_version integer,
    cost_micro_usd bigint,
    cost_upper_bound_micro_usd bigint,
    attempt integer DEFAULT 1 NOT NULL,
    schema_version text,
    prompt_version text,
    experiment_id text,
    error_code text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ai_usage_ledger_attempt_check CHECK ((attempt >= 1)),
    CONSTRAINT ai_usage_ledger_cache_write_tokens_check CHECK ((cache_write_tokens >= 0)),
    CONSTRAINT ai_usage_ledger_cached_input_tokens_check CHECK ((cached_input_tokens >= 0)),
    CONSTRAINT ai_usage_ledger_check CHECK (((status <> 'completed'::text) OR (price_catalog_version IS NOT NULL))),
    CONSTRAINT ai_usage_ledger_cost_micro_usd_check CHECK ((cost_micro_usd >= 0)),
    CONSTRAINT ai_usage_ledger_cost_upper_bound_micro_usd_check CHECK ((cost_upper_bound_micro_usd >= 0)),
    CONSTRAINT ai_usage_ledger_embedding_tokens_check CHECK ((embedding_tokens >= 0)),
    CONSTRAINT ai_usage_ledger_input_tokens_check CHECK ((input_tokens >= 0)),
    CONSTRAINT ai_usage_ledger_lane_check CHECK ((lane = ANY (ARRAY['foreground'::text, 'background'::text]))),
    CONSTRAINT ai_usage_ledger_latency_ms_check CHECK ((latency_ms >= 0)),
    CONSTRAINT ai_usage_ledger_output_tokens_check CHECK ((output_tokens >= 0)),
    CONSTRAINT ai_usage_ledger_status_check CHECK ((status = ANY (ARRAY['started'::text, 'completed'::text, 'unknown_usage'::text, 'failed'::text])))
);

-- TABLE: public.app_config
CREATE TABLE public.app_config (
    key text NOT NULL,
    value jsonb NOT NULL,
    description text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

-- TABLE: public.async_jobs
CREATE TABLE public.async_jobs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    queue text NOT NULL,
    job_type text NOT NULL,
    user_id uuid,
    dedup_key text NOT NULL,
    payload jsonb NOT NULL,
    state text DEFAULT 'ready'::text NOT NULL,
    priority integer DEFAULT 100 NOT NULL,
    available_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone,
    attempt integer DEFAULT 0 NOT NULL,
    max_attempts integer NOT NULL,
    lease_owner text,
    lease_token uuid,
    lease_until timestamp with time zone,
    result_code text,
    result_detail jsonb,
    last_error_code text,
    last_error_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    replay_of uuid,
    replay_operation_id uuid,
    payload_schema_version text DEFAULT 'job-payload-v1'::text NOT NULL,
    payload_hash text,
    payload_expires_at timestamp with time zone,
    payload_redacted_at timestamp with time zone,
    provider text,
    model text,
    lane text,
    eligible_at timestamp with time zone,
    CONSTRAINT async_jobs_attempt_check CHECK ((attempt >= 0)),
    CONSTRAINT async_jobs_check CHECK ((((state = 'running'::text) AND (lease_owner IS NOT NULL) AND (lease_token IS NOT NULL) AND (lease_until IS NOT NULL)) OR ((state <> 'running'::text) AND (lease_owner IS NULL) AND (lease_token IS NULL) AND (lease_until IS NULL)))),
    CONSTRAINT async_jobs_max_attempts_check CHECK ((max_attempts > 0)),
    CONSTRAINT async_jobs_state_check CHECK ((state = ANY (ARRAY['ready'::text, 'running'::text, 'succeeded'::text, 'dead'::text, 'cancelled'::text])))
);

-- TABLE: public.chat_active_turns
CREATE TABLE public.chat_active_turns (
    user_id uuid NOT NULL,
    turn_seq bigint NOT NULL,
    idempotency_key text NOT NULL,
    request_hash text NOT NULL,
    base_context_revision bigint NOT NULL,
    lease_token uuid NOT NULL,
    lease_until timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chat_active_turns_turn_seq_check CHECK ((turn_seq > 0))
);

-- TABLE: public.chat_contexts
CREATE TABLE public.chat_contexts (
    user_id uuid NOT NULL,
    anchor_message_id bigint DEFAULT 0 NOT NULL,
    memory_text text,
    memory_refreshed_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_active_at timestamp with time zone,
    memory_mode text DEFAULT 'legacy'::text NOT NULL,
    memory_generation bigint DEFAULT 0 NOT NULL,
    memory_source_watermark bigint DEFAULT 0 NOT NULL,
    relationship_profile_input_revision bigint DEFAULT 0 NOT NULL,
    context_revision bigint DEFAULT 0 NOT NULL,
    last_committed_turn_seq bigint DEFAULT 0 NOT NULL,
    prompt_cache_generation bigint DEFAULT 0 NOT NULL,
    anchor_revision bigint DEFAULT 0 NOT NULL,
    pending_anchor_message_id bigint,
    pending_plan_revision bigint,
    checkpoint_job_id uuid,
    checkpoint_source_hash text,
    CONSTRAINT chat_contexts_anchor_message_id_check CHECK ((anchor_message_id >= 0)),
    CONSTRAINT chat_contexts_memory_mode_check CHECK ((memory_mode = ANY (ARRAY['legacy'::text, 'normalized'::text])))
);

-- TABLE: public.chat_response_references
CREATE TABLE public.chat_response_references (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    reply_message_id bigint NOT NULL,
    ordinal integer NOT NULL,
    schema_version text DEFAULT 'diary-reference-v1'::text NOT NULL,
    domain text DEFAULT 'diary'::text NOT NULL,
    mode text NOT NULL,
    state text DEFAULT 'available'::text NOT NULL,
    diary_id uuid,
    rendered_metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    redacted_at timestamp with time zone,
    redaction_reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chat_response_references_check CHECK ((((state = 'available'::text) AND (diary_id IS NOT NULL) AND (redacted_at IS NULL)) OR ((state = 'unavailable'::text) AND (diary_id IS NULL)))),
    CONSTRAINT chat_response_references_domain_check CHECK ((domain = 'diary'::text)),
    CONSTRAINT chat_response_references_mode_check CHECK ((mode = ANY (ARRAY['full_card'::text, 'reopen_reference'::text]))),
    CONSTRAINT chat_response_references_ordinal_check CHECK (((ordinal >= 0) AND (ordinal <= 2))),
    CONSTRAINT chat_response_references_state_check CHECK ((state = ANY (ARRAY['available'::text, 'unavailable'::text])))
);

-- TABLE: public.conversation_checkpoints
CREATE TABLE public.conversation_checkpoints (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    through_message_id bigint NOT NULL,
    summary text NOT NULL,
    version text NOT NULL,
    source_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    memory_generation bigint DEFAULT 0 NOT NULL,
    kind text DEFAULT 'window'::text NOT NULL,
    segment_from_message_id bigint,
    segment_through_message_id bigint,
    coverage_from_message_id bigint,
    coverage_through_message_id bigint,
    previous_checkpoint_id uuid,
    locale text,
    source_started_at timestamp with time zone,
    source_ended_at timestamp with time zone,
    activity_date_from date,
    activity_date_to date,
    publish_state text DEFAULT 'published'::text NOT NULL,
    CONSTRAINT conversation_checkpoints_kind_check CHECK ((kind = ANY (ARRAY['window'::text, 'daily_digest'::text]))),
    CONSTRAINT conversation_checkpoints_publish_state_check CHECK ((publish_state = ANY (ARRAY['ready'::text, 'published'::text, 'superseded'::text])))
);

-- TABLE: public.conversation_focus
CREATE TABLE public.conversation_focus (
    user_id uuid NOT NULL,
    domain text NOT NULL,
    facet text,
    reference_ids uuid[] NOT NULL,
    context_revision bigint NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    expires_turn_seq bigint NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT conversation_focus_reference_ids_check CHECK (((cardinality(reference_ids) >= 1) AND (cardinality(reference_ids) <= 3)))
);

-- TABLE: public.daily_fortunes
CREATE TABLE public.daily_fortunes (
    user_id uuid NOT NULL,
    fortune_date date NOT NULL,
    timezone_snapshot text NOT NULL,
    profile_revision bigint NOT NULL,
    semantic_result jsonb NOT NULL,
    copy_by_locale jsonb NOT NULL,
    unlock_state text NOT NULL,
    unlock_source text,
    unlocked_at timestamp with time zone,
    revealed_at timestamp with time zone,
    ephemeris_version text NOT NULL,
    rule_version text NOT NULL,
    copy_version text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    result_schema_version integer DEFAULT 3 NOT NULL,
    CONSTRAINT daily_fortunes_copy_by_locale_check CHECK ((jsonb_typeof(copy_by_locale) = 'object'::text)),
    CONSTRAINT daily_fortunes_profile_revision_check CHECK ((profile_revision >= 1)),
    CONSTRAINT daily_fortunes_result_schema_version_ck CHECK ((result_schema_version >= 2)),
    CONSTRAINT daily_fortunes_semantic_result_check CHECK ((jsonb_typeof(semantic_result) = 'object'::text)),
    CONSTRAINT daily_fortunes_unlock_ck CHECK ((((unlock_state = 'locked'::text) AND (unlock_source IS NULL) AND (unlocked_at IS NULL) AND (revealed_at IS NULL)) OR ((unlock_state = 'unlocked'::text) AND (unlock_source IS NOT NULL) AND (unlocked_at IS NOT NULL)))),
    CONSTRAINT daily_fortunes_unlock_source_check CHECK ((unlock_source = ANY (ARRAY['subscription'::text, 'trial'::text, 'rewarded_ad'::text]))),
    CONSTRAINT daily_fortunes_unlock_state_check CHECK ((unlock_state = ANY (ARRAY['locked'::text, 'unlocked'::text])))
);

-- TABLE: public.diaries
CREATE TABLE public.diaries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    diary_date date NOT NULL,
    source text NOT NULL,
    preset_ment_id uuid,
    content text NOT NULL,
    weather text NOT NULL,
    published_at timestamp with time zone,
    first_read_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    kind text,
    activity_date date,
    display_date date NOT NULL,
    title text,
    author text DEFAULT 'capi'::text NOT NULL,
    occurred_at timestamp with time zone,
    occurred_timezone text,
    occurred_timezone_provenance text,
    primary_subject text,
    about_tags text[] DEFAULT '{}'::text[] NOT NULL,
    content_version integer DEFAULT 1 NOT NULL,
    record_status text DEFAULT 'published'::text NOT NULL,
    deleted_at timestamp with time zone,
    CONSTRAINT diaries_author_ck CHECK ((author = 'capi'::text)),
    CONSTRAINT diaries_kind_activity_ck CHECK ((((kind = 'welcome'::text) AND (activity_date IS NULL)) OR ((kind = ANY (ARRAY['shared_day'::text, 'capi_day'::text])) AND (activity_date IS NOT NULL)) OR (kind IS NULL))),
    CONSTRAINT diaries_kind_ck CHECK ((((record_status = 'processed'::text) AND (kind IS NULL)) OR ((record_status = ANY (ARRAY['draft'::text, 'published'::text])) AND (kind = ANY (ARRAY['welcome'::text, 'shared_day'::text, 'capi_day'::text]))) OR (record_status = 'deleted'::text))),
    CONSTRAINT diaries_source_check CHECK ((source = ANY (ARRAY['llm'::text, 'preset'::text, 'welcome'::text, 'none'::text]))),
    CONSTRAINT diaries_weather_check CHECK ((weather = ANY (ARRAY['sunny'::text, 'cloudy'::text, 'rainy'::text, 'windy'::text])))
);

-- TABLE: public.diary_claim_sources
CREATE TABLE public.diary_claim_sources (
    user_id uuid NOT NULL,
    diary_id uuid NOT NULL,
    message_id bigint NOT NULL,
    source_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

-- TABLE: public.diary_gen_claims
CREATE TABLE public.diary_gen_claims (
    user_id uuid NOT NULL,
    target_date date NOT NULL,
    claimed_at timestamp with time zone DEFAULT now() NOT NULL
);

-- TABLE: public.diary_generation_results
CREATE TABLE public.diary_generation_results (
    user_id uuid NOT NULL,
    target_date date NOT NULL,
    status text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    preset_ment_id uuid,
    CONSTRAINT diary_generation_results_status_check CHECK ((status = ANY (ARRAY['no_entry'::text, 'preset'::text]))),
    CONSTRAINT diary_generation_results_preset_shape_check CHECK ((((status = 'preset'::text) AND (preset_ment_id IS NOT NULL)) OR ((status = 'no_entry'::text) AND (preset_ment_id IS NULL))))
);

-- TABLE: public.diary_recall_documents
CREATE TABLE public.diary_recall_documents (
    user_id uuid NOT NULL,
    diary_id uuid NOT NULL,
    search_text text NOT NULL,
    source_hash text NOT NULL,
    embedding public.vector(1536),
    suppression_generation bigint NOT NULL,
    index_version text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    embedding_model text DEFAULT 'text-embedding-3-small'::text NOT NULL,
    embedding_repair_attempts smallint DEFAULT 0 NOT NULL,
    CONSTRAINT diary_recall_repair_attempts_ck CHECK (((embedding_repair_attempts >= 0) AND (embedding_repair_attempts <= 3)))
);

-- TABLE: public.feedback
CREATE TABLE public.feedback (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    message text NOT NULL,
    contact text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT feedback_contact_check CHECK ((char_length(contact) <= 200)),
    CONSTRAINT feedback_message_check CHECK ((char_length(message) <= 2000))
);

-- TABLE: public.fortune_ad_sessions
CREATE TABLE public.fortune_ad_sessions (
    session_id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    fortune_date date NOT NULL,
    client_request_id uuid NOT NULL,
    verified boolean DEFAULT false NOT NULL,
    ssv_transaction_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    verified_at timestamp with time zone,
    CONSTRAINT fortune_ad_sessions_expiry_ck CHECK ((expires_at > created_at)),
    CONSTRAINT fortune_ad_sessions_verified_ck CHECK ((((verified = false) AND (ssv_transaction_id IS NULL) AND (verified_at IS NULL)) OR ((verified = true) AND (ssv_transaction_id IS NOT NULL) AND (verified_at IS NOT NULL))))
);

-- TABLE: public.fortune_profiles
CREATE TABLE public.fortune_profiles (
    user_id uuid NOT NULL,
    gender text NOT NULL,
    birth_date date NOT NULL,
    revision bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT fortune_profiles_gender_ck CHECK ((gender = ANY (ARRAY['man'::text, 'woman'::text, 'undisclosed'::text]))),
    CONSTRAINT fortune_profiles_revision_check CHECK ((revision >= 1))
);

-- TABLE: public.greetings
CREATE TABLE public.greetings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    context text NOT NULL,
    content text NOT NULL,
    activity_date date NOT NULL,
    committed_message_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT greetings_context_check CHECK ((context = ANY (ARRAY['onboarding'::text, 'home_enter'::text, 'morning'::text, 'evening'::text, 'comeback'::text])))
);

-- TABLE: public.hay_transactions
CREATE TABLE public.hay_transactions (
    id bigint NOT NULL,
    user_id uuid NOT NULL,
    type text NOT NULL,
    amount integer NOT NULL,
    balance_after integer NOT NULL,
    order_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT hay_transactions_amount_check CHECK ((amount <> 0)),
    CONSTRAINT hay_transactions_type_check CHECK ((type = ANY (ARRAY['attendance'::text, 'ad_reward'::text, 'routine_reward'::text, 'iap_purchase'::text, 'subscription_grant'::text, 'shop_purchase'::text, 'refund_revoke'::text, 'admin_adjustment'::text])))
);

-- SEQUENCE: public.hay_transactions_id_seq
ALTER TABLE public.hay_transactions ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.hay_transactions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

-- TABLE: public.idempotency_keys
CREATE TABLE public.idempotency_keys (
    user_id uuid NOT NULL,
    key text NOT NULL,
    response jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    request_hash text,
    response_schema_version bigint DEFAULT 1 NOT NULL,
    reply_message_id bigint,
    terminal_status text DEFAULT 'succeeded'::text NOT NULL,
    response_expires_at timestamp with time zone,
    dedupe_expires_at timestamp with time zone,
    redacted_at timestamp with time zone,
    CONSTRAINT idempotency_terminal_status_ck CHECK ((terminal_status = ANY (ARRAY['succeeded'::text, 'expired'::text, 'redacted'::text])))
);

-- TABLE: public.job_attempts
CREATE TABLE public.job_attempts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid NOT NULL,
    attempt integer NOT NULL,
    queue text NOT NULL,
    job_type text NOT NULL,
    worker_id text,
    lease_token uuid,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    duration_ms integer,
    outcome text,
    error_code text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT job_attempts_attempt_check CHECK ((attempt >= 1)),
    CONSTRAINT job_attempts_duration_ms_check CHECK ((duration_ms >= 0)),
    CONSTRAINT job_attempts_outcome_check CHECK (((outcome IS NULL) OR (outcome = ANY (ARRAY['succeeded'::text, 'retryable'::text, 'dead'::text, 'cancelled'::text, 'lease_lost'::text, 'timeout'::text]))))
);

-- TABLE: public.mem0_ingest_candidate_sources
CREATE TABLE public.mem0_ingest_candidate_sources (
    candidate_id uuid NOT NULL,
    user_id uuid NOT NULL,
    source_message_id bigint NOT NULL,
    source_sender text NOT NULL,
    source_content_hash text NOT NULL,
    evidence_start_utf8 integer NOT NULL,
    evidence_end_utf8 integer NOT NULL,
    authority text NOT NULL,
    confidence double precision,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT mem0_ingest_candidate_sources_authority_check CHECK ((authority = ANY (ARRAY['explicit_user'::text, 'confirmed_user'::text]))),
    CONSTRAINT mem0_ingest_candidate_sources_check CHECK (((0 <= evidence_start_utf8) AND (evidence_start_utf8 < evidence_end_utf8))),
    CONSTRAINT mem0_ingest_candidate_sources_confidence_check CHECK (((confidence >= (0)::double precision) AND (confidence <= (1)::double precision))),
    CONSTRAINT mem0_ingest_candidate_sources_source_sender_check CHECK ((source_sender = 'user'::text))
);

-- TABLE: public.mem0_ingest_candidates
CREATE TABLE public.mem0_ingest_candidates (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    turn_seq bigint NOT NULL,
    candidate_hash text NOT NULL,
    schema_version text NOT NULL,
    extractor_version text NOT NULL,
    normalizer_version text NOT NULL,
    provider_memory_id uuid NOT NULL,
    candidate_text text NOT NULL,
    temporal_proposal_json jsonb,
    event_started_at timestamp with time zone,
    event_ended_at timestamp with time zone,
    event_time_precision text,
    resolved_timezone text,
    status text DEFAULT 'planned'::text NOT NULL,
    repair_generation integer DEFAULT 0 NOT NULL,
    scrubbed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    category text,
    CONSTRAINT mem0_ingest_candidates_status_check CHECK ((status = ANY (ARRAY['planned'::text, 'committed'::text, 'dead'::text])))
);

-- COMMENT: public.COLUMN mem0_ingest_candidates.category
COMMENT ON COLUMN public.mem0_ingest_candidates.category IS '기억의 종류. 허용 목록은 코드가 갖는다(mem0_extractor.CATEGORIES). NULL = v3 이전에 뽑힌 기억.';

-- TABLE: public.mem0_memory_registry
CREATE TABLE public.mem0_memory_registry (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    provider text NOT NULL,
    collection_version text NOT NULL,
    provider_memory_id uuid NOT NULL,
    source_turn_seq bigint NOT NULL,
    content_hash text NOT NULL,
    event_started_at timestamp with time zone,
    event_ended_at timestamp with time zone,
    event_time_precision text,
    resolved_timezone text,
    temporal_resolver_version text,
    semantic_status text DEFAULT 'pending'::text NOT NULL,
    provider_delete_state text DEFAULT 'kept'::text NOT NULL,
    provider_deleted_at timestamp with time zone,
    conflict_group_id uuid,
    duplicate_of_registry_id uuid,
    superseded_by_registry_id uuid,
    classification_version text,
    schema_version text NOT NULL,
    revision bigint DEFAULT 0 NOT NULL,
    last_confirmed_at timestamp with time zone,
    source_count integer DEFAULT 0 NOT NULL,
    max_source_confidence double precision,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    category text,
    last_reconsolidated_at timestamp with time zone,
    CONSTRAINT mem0_memory_registry_provider_delete_state_check CHECK ((provider_delete_state = ANY (ARRAY['kept'::text, 'pending'::text, 'deleted'::text, 'failed'::text]))),
    CONSTRAINT mem0_memory_registry_semantic_status_check CHECK ((semantic_status = ANY (ARRAY['pending'::text, 'active'::text, 'duplicate'::text, 'superseded'::text, 'ambiguous'::text, 'excluded'::text, 'rejected_policy'::text]))),
    CONSTRAINT mem0_memory_registry_source_count_check CHECK ((source_count >= 0))
);

-- COMMENT: public.COLUMN mem0_memory_registry.category
COMMENT ON COLUMN public.mem0_memory_registry.category IS '기억의 종류. 회상에서 오래 남는 종류를 앞세우는 데 쓴다. NULL = v3 이전에 뽑힌 기억.';

-- COMMENT: public.COLUMN mem0_memory_registry.last_reconsolidated_at
COMMENT ON COLUMN public.mem0_memory_registry.last_reconsolidated_at IS '마지막으로 재판정 비교에 참여한 시각. NULL = 아직 한 번도 안 봤다.';

-- TABLE: public.mem0_memory_sources
CREATE TABLE public.mem0_memory_sources (
    registry_id uuid NOT NULL,
    user_id uuid NOT NULL,
    source_turn_seq bigint NOT NULL,
    source_message_id bigint NOT NULL,
    source_sender text NOT NULL,
    evidence_start_utf8 integer NOT NULL,
    evidence_end_utf8 integer NOT NULL,
    source_content_hash text NOT NULL,
    source_occurred_at timestamp with time zone NOT NULL,
    source_activity_date date NOT NULL,
    authority text NOT NULL,
    confidence double precision,
    extractor_version text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT mem0_memory_sources_authority_check CHECK ((authority = ANY (ARRAY['explicit_user'::text, 'confirmed_user'::text]))),
    CONSTRAINT mem0_memory_sources_check CHECK (((0 <= evidence_start_utf8) AND (evidence_start_utf8 < evidence_end_utf8))),
    CONSTRAINT mem0_memory_sources_confidence_check CHECK (((confidence >= (0)::double precision) AND (confidence <= (1)::double precision))),
    CONSTRAINT mem0_memory_sources_source_sender_check CHECK ((source_sender = 'user'::text))
);

-- TABLE: public.memory_pipeline_states
CREATE TABLE public.memory_pipeline_states (
    user_id uuid NOT NULL,
    source_through_turn_seq bigint DEFAULT 0 NOT NULL,
    ingest_through_turn_seq bigint DEFAULT 0 NOT NULL,
    consolidated_through_turn_seq bigint DEFAULT 0 NOT NULL,
    active_job_id uuid,
    stage_token uuid,
    lease_until timestamp with time zone,
    revision bigint DEFAULT 0 NOT NULL,
    privacy_epoch bigint DEFAULT 0 NOT NULL,
    repair_generation integer DEFAULT 0 NOT NULL,
    bootstrap_status text DEFAULT 'legacy'::text NOT NULL,
    historical_upper_turn_seq bigint,
    mode text DEFAULT 'legacy'::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT memory_pipeline_states_bootstrap_status_check CHECK ((bootstrap_status = ANY (ARRAY['legacy'::text, 'collecting'::text, 'ready'::text]))),
    CONSTRAINT memory_pipeline_states_check CHECK ((ingest_through_turn_seq <= source_through_turn_seq)),
    CONSTRAINT memory_pipeline_states_check1 CHECK ((consolidated_through_turn_seq <= ingest_through_turn_seq)),
    CONSTRAINT memory_pipeline_states_consolidated_through_turn_seq_check CHECK ((consolidated_through_turn_seq >= 0)),
    CONSTRAINT memory_pipeline_states_historical_upper_turn_seq_check CHECK ((historical_upper_turn_seq >= 0)),
    CONSTRAINT memory_pipeline_states_ingest_through_turn_seq_check CHECK ((ingest_through_turn_seq >= 0)),
    CONSTRAINT memory_pipeline_states_mode_check CHECK ((mode = ANY (ARRAY['legacy'::text, 'shadow'::text, 'v2'::text]))),
    CONSTRAINT memory_pipeline_states_privacy_epoch_check CHECK ((privacy_epoch >= 0)),
    CONSTRAINT memory_pipeline_states_repair_generation_check CHECK ((repair_generation >= 0)),
    CONSTRAINT memory_pipeline_states_revision_check CHECK ((revision >= 0)),
    CONSTRAINT memory_pipeline_states_source_through_turn_seq_check CHECK ((source_through_turn_seq >= 0))
);

-- TABLE: public.messages
CREATE TABLE public.messages (
    id bigint NOT NULL,
    user_id uuid NOT NULL,
    sender text NOT NULL,
    kind text DEFAULT 'normal'::text NOT NULL,
    content text NOT NULL,
    input_tokens integer,
    output_tokens integer,
    cache_read_tokens integer,
    cache_write_tokens integer,
    billable_tokens integer,
    activity_date date NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    turn_seq bigint,
    turn_position smallint,
    CONSTRAINT messages_kind_check CHECK ((kind = ANY (ARRAY['normal'::text, 'greeting'::text, 'fortune_context_root'::text, 'fortune_derived'::text, 'topic_opening'::text]))),
    CONSTRAINT messages_sender_check CHECK ((sender = ANY (ARRAY['user'::text, 'moly'::text]))),
    CONSTRAINT messages_turn_position_ck CHECK ((((turn_seq IS NULL) AND (turn_position IS NULL)) OR ((turn_seq > 0) AND ((turn_position >= 0) AND (turn_position <= 2)))))
);

-- SEQUENCE: public.messages_id_seq
ALTER TABLE public.messages ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.messages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

-- TABLE: public.moly_life_ments
CREATE TABLE public.moly_life_ments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    content text NOT NULL,
    weather text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    diary_date date,
    week_start_date date,
    sequence_no integer,
    CONSTRAINT moly_life_ments_week_shape_check CHECK ((((week_start_date IS NULL) AND (sequence_no IS NULL)) OR ((week_start_date IS NOT NULL) AND (sequence_no IS NOT NULL) AND (diary_date IS NULL) AND (EXTRACT(isodow FROM week_start_date) = 1) AND (sequence_no > 0)))),
    CONSTRAINT moly_life_ments_weather_check CHECK ((weather = ANY (ARRAY['sunny'::text, 'cloudy'::text, 'rainy'::text, 'windy'::text])))
);

-- TABLE: public.mood_entries
CREATE TABLE public.mood_entries (
    user_id uuid NOT NULL,
    entry_date date NOT NULL,
    kind text NOT NULL,
    note text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

-- TABLE: public.order_items
CREATE TABLE public.order_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    order_id uuid NOT NULL,
    product_id uuid NOT NULL,
    quantity integer DEFAULT 1 NOT NULL,
    unit_price integer NOT NULL,
    CONSTRAINT order_items_quantity_check CHECK ((quantity > 0)),
    CONSTRAINT order_items_unit_price_check CHECK ((unit_price >= 0))
);

-- TABLE: public.orders
CREATE TABLE public.orders (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    currency text NOT NULL,
    status text NOT NULL,
    total_amount integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT orders_currency_check CHECK ((currency = ANY (ARRAY['KRW'::text, 'HAY'::text]))),
    CONSTRAINT orders_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'paid'::text, 'failed'::text, 'refunded'::text]))),
    CONSTRAINT orders_total_amount_check CHECK ((total_amount >= 0))
);

-- TABLE: public.payments
CREATE TABLE public.payments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    order_id uuid,
    subscription_id uuid,
    store text NOT NULL,
    store_transaction_id text NOT NULL,
    amount numeric(14,4),
    currency text,
    status text NOT NULL,
    paid_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT payments_status_check CHECK ((status = ANY (ARRAY['paid'::text, 'refunded'::text]))),
    CONSTRAINT payments_target_ck CHECK (((order_id IS NOT NULL) OR (subscription_id IS NOT NULL)))
);

-- TABLE: public.privacy_ledger_events
CREATE TABLE public.privacy_ledger_events (
    id bigint NOT NULL,
    operation_id uuid NOT NULL,
    user_id uuid NOT NULL,
    event text NOT NULL,
    high_watermark bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

-- SEQUENCE: public.privacy_ledger_events_id_seq
ALTER TABLE public.privacy_ledger_events ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.privacy_ledger_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

-- TABLE: public.privacy_subject_barriers
CREATE TABLE public.privacy_subject_barriers (
    user_id uuid NOT NULL,
    state text NOT NULL,
    operation_id uuid,
    high_watermark bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    epoch bigint DEFAULT 0 NOT NULL,
    CONSTRAINT privacy_subject_barriers_epoch_check CHECK ((epoch >= 0)),
    CONSTRAINT privacy_subject_barriers_operation_ck CHECK (((state = 'active'::text) OR (operation_id IS NOT NULL))),
    CONSTRAINT privacy_subject_barriers_state_check CHECK ((state = ANY (ARRAY['active'::text, 'deleting'::text, 'deleted'::text])))
);

-- TABLE: public.products
CREATE TABLE public.products (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    product_type text NOT NULL,
    name text NOT NULL,
    description text,
    slot text,
    price_hay integer,
    is_subscriber_only boolean DEFAULT false NOT NULL,
    assets jsonb,
    hay_amount integer,
    price_krw integer,
    app_store_product_id text,
    is_active boolean DEFAULT true NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    public_id text,
    asset_version integer,
    is_v2_only boolean DEFAULT false NOT NULL,
    play_store_product_id text,
    name_i18n jsonb,
    CONSTRAINT products_cosmetic_ck CHECK (((product_type <> 'cosmetic'::text) OR ((public_id IS NOT NULL) AND (slot IS NOT NULL) AND (hay_amount IS NULL) AND (app_store_product_id IS NULL) AND (price_krw IS NULL) AND (play_store_product_id IS NULL) AND (is_subscriber_only = false) AND ((is_active = false) OR ((asset_version IS NOT NULL) AND (asset_version >= 1) AND (assets IS NOT NULL)))))),
    CONSTRAINT products_hay_pack_ck CHECK (((product_type <> 'hay_pack'::text) OR ((hay_amount IS NOT NULL) AND (app_store_product_id IS NOT NULL) AND (slot IS NULL) AND (price_hay IS NULL) AND (assets IS NULL) AND (is_subscriber_only = false)))),
    CONSTRAINT products_name_i18n_obj_ck CHECK (((name_i18n IS NULL) OR (jsonb_typeof(name_i18n) = 'object'::text))),
    CONSTRAINT products_price_hay_positive_ck CHECK ((price_hay >= 1)),
    CONSTRAINT products_product_type_check CHECK ((product_type = ANY (ARRAY['hay_pack'::text, 'cosmetic'::text]))),
    CONSTRAINT products_slot_check CHECK ((slot = ANY (ARRAY['theme'::text, 'hat'::text, 'glasses'::text, 'neck'::text, 'body'::text])))
);

-- TABLE: public.profiles
CREATE TABLE public.profiles (
    id uuid NOT NULL,
    nickname text,
    language text DEFAULT 'en'::text NOT NULL,
    timezone text DEFAULT 'Asia/Seoul'::text NOT NULL,
    hay_balance integer DEFAULT 0 NOT NULL,
    trial_ends_at timestamp with time zone,
    review_prompted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    relationship_started_at timestamp with time zone,
    relationship_started_timezone text,
    relationship_display_date date,
    next_diary_due_at timestamp with time zone,
    relationship_revision bigint DEFAULT 0 NOT NULL,
    CONSTRAINT profiles_nickname_check CHECK ((char_length(nickname) <= 10)),
    CONSTRAINT profiles_relationship_origin_ck CHECK ((num_nonnulls(relationship_started_at, relationship_started_timezone, relationship_display_date) = ANY (ARRAY[0, 3]))),
    CONSTRAINT profiles_relationship_revision_check CHECK ((relationship_revision >= 0))
);

-- TABLE: public.provider_backoffs
CREATE TABLE public.provider_backoffs (
    provider text NOT NULL,
    model text NOT NULL,
    lane text NOT NULL,
    blocked_until timestamp with time zone NOT NULL,
    reason text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

-- TABLE: public.relationship_events
CREATE TABLE public.relationship_events (
    id bigint NOT NULL,
    user_id uuid NOT NULL,
    event_type text NOT NULL,
    source_id text,
    activity_date date NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    delta jsonb,
    dedup_key text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    turn_seq bigint,
    CONSTRAINT relationship_events_turn_seq_check CHECK (((turn_seq IS NULL) OR (turn_seq >= 0))),
    CONSTRAINT relationship_events_type_ck CHECK ((event_type = ANY (ARRAY['normal_turn_committed'::text, 'active_day_started'::text])))
);

-- SEQUENCE: public.relationship_events_id_seq
ALTER TABLE public.relationship_events ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.relationship_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

-- TABLE: public.relationship_profile_renders
CREATE TABLE public.relationship_profile_renders (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    prompt_revision bigint NOT NULL,
    profile_relationship_revision bigint NOT NULL,
    locale text NOT NULL,
    renderer_version text NOT NULL,
    rendered_text text NOT NULL,
    render_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

-- TABLE: public.revenuecat_events
CREATE TABLE public.revenuecat_events (
    event_id text NOT NULL,
    payload jsonb NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    processed_at timestamp with time zone,
    last_error text,
    CONSTRAINT revenuecat_events_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'processed'::text, 'failed'::text])))
);

-- TABLE: public.reward_ad_sessions
CREATE TABLE public.reward_ad_sessions (
    session_id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    activity_date date NOT NULL,
    ssv_transaction_id text,
    granted boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone DEFAULT (now() + '00:30:00'::interval) NOT NULL,
    CONSTRAINT reward_ad_sessions_expiry_ck CHECK ((expires_at > created_at))
);

-- TABLE: public.routine_completions
CREATE TABLE public.routine_completions (
    id bigint NOT NULL,
    routine_id uuid NOT NULL,
    user_id uuid NOT NULL,
    activity_date date NOT NULL,
    completed_at timestamp with time zone DEFAULT now() NOT NULL
);

-- SEQUENCE: public.routine_completions_id_seq
ALTER TABLE public.routine_completions ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.routine_completions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

-- TABLE: public.routines
CREATE TABLE public.routines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    name text NOT NULL,
    frequency_per_week smallint NOT NULL,
    days_of_week smallint[] NOT NULL,
    reminder_enabled boolean DEFAULT false NOT NULL,
    reminder_time time without time zone,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    name_i18n jsonb,
    CONSTRAINT routines_name_i18n_obj_ck CHECK (((name_i18n IS NULL) OR (jsonb_typeof(name_i18n) = 'object'::text)))
);

-- TABLE: public.schema_migrations
CREATE TABLE public.schema_migrations (
    migration_name text NOT NULL,
    checksum_sha256 text NOT NULL,
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    applied_by text DEFAULT CURRENT_USER NOT NULL
);

-- TABLE: public.shadow_prompt_traces
CREATE TABLE public.shadow_prompt_traces (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    turn_seq bigint NOT NULL,
    assembler_version text NOT NULL,
    total_bytes integer NOT NULL,
    cacheable_bytes integer NOT NULL,
    volatile_bytes integer NOT NULL,
    message_count integer NOT NULL,
    segment_counts jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT shadow_prompt_traces_cacheable_bytes_check CHECK ((cacheable_bytes >= 0)),
    CONSTRAINT shadow_prompt_traces_message_count_check CHECK ((message_count >= 0)),
    CONSTRAINT shadow_prompt_traces_total_bytes_check CHECK ((total_bytes >= 0)),
    CONSTRAINT shadow_prompt_traces_turn_seq_check CHECK ((turn_seq > 0)),
    CONSTRAINT shadow_prompt_traces_volatile_bytes_check CHECK ((volatile_bytes >= 0))
);

-- TABLE: public.subscription_hay_grants
CREATE TABLE public.subscription_hay_grants (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    plan text NOT NULL,
    hay_transaction_id bigint,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone,
    clawback_hay_transaction_id bigint,
    CONSTRAINT subscription_hay_grants_plan_check CHECK ((plan = ANY (ARRAY['monthly'::text, 'yearly'::text])))
);

-- TABLE: public.subscriptions
CREATE TABLE public.subscriptions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    plan text NOT NULL,
    status text NOT NULL,
    original_transaction_id text NOT NULL,
    latest_transaction_id text,
    purchased_at timestamp with time zone,
    expires_at timestamp with time zone,
    auto_renew_enabled boolean DEFAULT true NOT NULL,
    environment text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_event_at timestamp with time zone,
    CONSTRAINT subscriptions_plan_check CHECK ((plan = ANY (ARRAY['monthly'::text, 'yearly'::text]))),
    CONSTRAINT subscriptions_status_check CHECK ((status = ANY (ARRAY['active'::text, 'grace_period'::text, 'expired'::text, 'revoked'::text])))
);

-- TABLE: public.user_daily_stats
CREATE TABLE public.user_daily_stats (
    id bigint NOT NULL,
    user_id uuid NOT NULL,
    activity_date date NOT NULL,
    tokens_used integer DEFAULT 0 NOT NULL,
    ad_reward_count smallint DEFAULT 0 NOT NULL,
    attendance_claimed_at timestamp with time zone,
    routine_reward_claimed_at timestamp with time zone,
    morning_notified_at timestamp with time zone,
    evening_notified_at timestamp with time zone
);

-- SEQUENCE: public.user_daily_stats_id_seq
ALTER TABLE public.user_daily_stats ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.user_daily_stats_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

-- TABLE: public.user_devices
CREATE TABLE public.user_devices (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    platform text NOT NULL,
    push_token text NOT NULL,
    last_active_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_devices_platform_check CHECK ((platform = ANY (ARRAY['ios'::text, 'android'::text])))
);

-- TABLE: public.user_interaction_contract_items
CREATE TABLE public.user_interaction_contract_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    contract_id uuid NOT NULL,
    user_id uuid NOT NULL,
    item_key text NOT NULL,
    section text NOT NULL,
    value_json jsonb NOT NULL,
    rendered_text text NOT NULL,
    authority text NOT NULL,
    confidence double precision,
    effective_from timestamp with time zone DEFAULT now() NOT NULL,
    effective_to timestamp with time zone,
    status text DEFAULT 'active'::text NOT NULL,
    source_message_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_interaction_contract_items_authority_check CHECK ((authority = ANY (ARRAY['explicit_user'::text, 'confirmed'::text, 'repeated_observation'::text]))),
    CONSTRAINT user_interaction_contract_items_confidence_check CHECK (((confidence >= (0)::double precision) AND (confidence <= (1)::double precision))),
    CONSTRAINT user_interaction_contract_items_section_check CHECK ((section = ANY (ARRAY['address_policy'::text, 'communication_style'::text, 'comfort_style'::text, 'boundaries'::text, 'relationship_frame'::text, 'durable_commitments'::text]))),
    CONSTRAINT user_interaction_contract_items_status_check CHECK ((status = ANY (ARRAY['active'::text, 'superseded'::text, 'rejected'::text])))
);

-- TABLE: public.user_interaction_contracts
CREATE TABLE public.user_interaction_contracts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    version integer NOT NULL,
    locale text NOT NULL,
    document_json jsonb NOT NULL,
    rendered_text text NOT NULL,
    render_hash text NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    source_watermark bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    published_at timestamp with time zone,
    CONSTRAINT user_interaction_contracts_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'published'::text, 'superseded'::text, 'rejected'::text]))),
    CONSTRAINT user_interaction_contracts_version_check CHECK ((version > 0))
);

-- TABLE: public.user_items
CREATE TABLE public.user_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    product_id uuid NOT NULL,
    source text DEFAULT 'purchase'::text NOT NULL,
    order_id uuid,
    equipped_slot text,
    equipped_at timestamp with time zone,
    acquired_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_items_equipped_ck CHECK (((equipped_at IS NULL) OR (equipped_slot IS NOT NULL))),
    CONSTRAINT user_items_equipped_slot_check CHECK ((equipped_slot = ANY (ARRAY['theme'::text, 'hat'::text, 'glasses'::text, 'neck'::text, 'body'::text]))),
    CONSTRAINT user_items_source_check CHECK ((source = ANY (ARRAY['purchase'::text, 'subscription'::text, 'admin_grant'::text])))
);

-- TABLE: public.user_notification_settings
CREATE TABLE public.user_notification_settings (
    user_id uuid NOT NULL,
    type text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    CONSTRAINT user_notification_settings_type_check CHECK ((type = ANY (ARRAY['morning_diary'::text, 'evening_chat'::text])))
);

-- TABLE: public.user_relationship_states
CREATE TABLE public.user_relationship_states (
    user_id uuid NOT NULL,
    relationship_started_at timestamp with time zone,
    active_days integer DEFAULT 0 NOT NULL,
    successful_turns bigint DEFAULT 0 NOT NULL,
    qualifying_turns bigint DEFAULT 0 NOT NULL,
    last_interaction_at timestamp with time zone,
    relationship_stage text DEFAULT 'new'::text NOT NULL,
    stage_rule_version text DEFAULT 'relationship-v1'::text NOT NULL,
    latest_event_id bigint,
    version bigint DEFAULT 0 NOT NULL,
    prompt_revision bigint DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_relationship_states_active_days_check CHECK ((active_days >= 0)),
    CONSTRAINT user_relationship_states_qualifying_turns_check CHECK ((qualifying_turns >= 0)),
    CONSTRAINT user_relationship_states_relationship_stage_check CHECK ((relationship_stage = ANY (ARRAY['new'::text, 'acquainted'::text, 'familiar'::text, 'close'::text]))),
    CONSTRAINT user_relationship_states_successful_turns_check CHECK ((successful_turns >= 0))
);

-- TABLE: public.user_schedules
CREATE TABLE public.user_schedules (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    kind text NOT NULL,
    timezone_snapshot text NOT NULL,
    next_due_at timestamp with time zone NOT NULL,
    revision bigint DEFAULT 0 NOT NULL,
    last_run_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_schedules_kind_check CHECK ((kind = ANY (ARRAY['diary_generate'::text, 'diary_morning_notification'::text, 'evening_checkin'::text]))),
    CONSTRAINT user_schedules_revision_check CHECK ((revision >= 0))
);

-- TABLE: vecs.moly_memories_v2
CREATE TABLE vecs.moly_memories_v2 (
    id character varying NOT NULL,
    vec public.vector(1536) NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
)
WITH (autovacuum_vacuum_scale_factor='0.02', toast.autovacuum_vacuum_scale_factor='0.02');

-- CONSTRAINT: public.ai_price_catalog ai_price_catalog_catalog_version_provider_model_key
ALTER TABLE ONLY public.ai_price_catalog
    ADD CONSTRAINT ai_price_catalog_catalog_version_provider_model_key UNIQUE (catalog_version, provider, model);

-- CONSTRAINT: public.ai_price_catalog ai_price_catalog_pkey
ALTER TABLE ONLY public.ai_price_catalog
    ADD CONSTRAINT ai_price_catalog_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.ai_usage_daily_rollup ai_usage_daily_rollup_pkey
ALTER TABLE ONLY public.ai_usage_daily_rollup
    ADD CONSTRAINT ai_usage_daily_rollup_pkey PRIMARY KEY (kst_date, provider, model, lane, purpose, status);

-- CONSTRAINT: public.ai_usage_ledger ai_usage_ledger_pkey
ALTER TABLE ONLY public.ai_usage_ledger
    ADD CONSTRAINT ai_usage_ledger_pkey PRIMARY KEY (call_id);

-- CONSTRAINT: public.app_config app_config_pkey
ALTER TABLE ONLY public.app_config
    ADD CONSTRAINT app_config_pkey PRIMARY KEY (key);

-- CONSTRAINT: public.async_jobs async_jobs_job_type_dedup_key_key
ALTER TABLE ONLY public.async_jobs
    ADD CONSTRAINT async_jobs_job_type_dedup_key_key UNIQUE (job_type, dedup_key);

-- CONSTRAINT: public.async_jobs async_jobs_pkey
ALTER TABLE ONLY public.async_jobs
    ADD CONSTRAINT async_jobs_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.chat_active_turns chat_active_turns_pkey
ALTER TABLE ONLY public.chat_active_turns
    ADD CONSTRAINT chat_active_turns_pkey PRIMARY KEY (user_id);

-- CONSTRAINT: public.chat_contexts chat_contexts_pkey
ALTER TABLE ONLY public.chat_contexts
    ADD CONSTRAINT chat_contexts_pkey PRIMARY KEY (user_id);

-- CONSTRAINT: public.chat_response_references chat_response_references_pkey
ALTER TABLE ONLY public.chat_response_references
    ADD CONSTRAINT chat_response_references_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.chat_response_references chat_response_references_user_id_reply_message_id_ordinal_key
ALTER TABLE ONLY public.chat_response_references
    ADD CONSTRAINT chat_response_references_user_id_reply_message_id_ordinal_key UNIQUE (user_id, reply_message_id, ordinal);

-- CONSTRAINT: public.conversation_checkpoints conversation_checkpoints_pkey
ALTER TABLE ONLY public.conversation_checkpoints
    ADD CONSTRAINT conversation_checkpoints_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.conversation_checkpoints conversation_checkpoints_user_id_through_message_id_source__key
ALTER TABLE ONLY public.conversation_checkpoints
    ADD CONSTRAINT conversation_checkpoints_user_id_through_message_id_source__key UNIQUE (user_id, through_message_id, source_hash);

-- CONSTRAINT: public.conversation_focus conversation_focus_pkey
ALTER TABLE ONLY public.conversation_focus
    ADD CONSTRAINT conversation_focus_pkey PRIMARY KEY (user_id);

-- CONSTRAINT: public.daily_fortunes daily_fortunes_pkey
ALTER TABLE ONLY public.daily_fortunes
    ADD CONSTRAINT daily_fortunes_pkey PRIMARY KEY (user_id);

-- CONSTRAINT: public.diaries diaries_pkey
ALTER TABLE ONLY public.diaries
    ADD CONSTRAINT diaries_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.diary_claim_sources diary_claim_sources_pkey
ALTER TABLE ONLY public.diary_claim_sources
    ADD CONSTRAINT diary_claim_sources_pkey PRIMARY KEY (user_id, diary_id, message_id);

-- CONSTRAINT: public.diary_gen_claims diary_gen_claims_pkey
ALTER TABLE ONLY public.diary_gen_claims
    ADD CONSTRAINT diary_gen_claims_pkey PRIMARY KEY (user_id, target_date);

-- CONSTRAINT: public.diary_generation_results diary_generation_results_pkey
ALTER TABLE ONLY public.diary_generation_results
    ADD CONSTRAINT diary_generation_results_pkey PRIMARY KEY (user_id, target_date);

-- CONSTRAINT: public.diary_recall_documents diary_recall_documents_pkey
ALTER TABLE ONLY public.diary_recall_documents
    ADD CONSTRAINT diary_recall_documents_pkey PRIMARY KEY (user_id, diary_id);

-- CONSTRAINT: public.feedback feedback_pkey
ALTER TABLE ONLY public.feedback
    ADD CONSTRAINT feedback_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.fortune_ad_sessions fortune_ad_sessions_client_uq
ALTER TABLE ONLY public.fortune_ad_sessions
    ADD CONSTRAINT fortune_ad_sessions_client_uq UNIQUE (user_id, client_request_id);

-- CONSTRAINT: public.fortune_ad_sessions fortune_ad_sessions_pkey
ALTER TABLE ONLY public.fortune_ad_sessions
    ADD CONSTRAINT fortune_ad_sessions_pkey PRIMARY KEY (session_id);

-- CONSTRAINT: public.fortune_ad_sessions fortune_ad_sessions_ssv_transaction_id_key
ALTER TABLE ONLY public.fortune_ad_sessions
    ADD CONSTRAINT fortune_ad_sessions_ssv_transaction_id_key UNIQUE (ssv_transaction_id);

-- CONSTRAINT: public.fortune_profiles fortune_profiles_pkey
ALTER TABLE ONLY public.fortune_profiles
    ADD CONSTRAINT fortune_profiles_pkey PRIMARY KEY (user_id);

-- CONSTRAINT: public.greetings greetings_pkey
ALTER TABLE ONLY public.greetings
    ADD CONSTRAINT greetings_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.greetings greetings_user_ctx_date_uq
ALTER TABLE ONLY public.greetings
    ADD CONSTRAINT greetings_user_ctx_date_uq UNIQUE (user_id, context, activity_date);

-- CONSTRAINT: public.hay_transactions hay_transactions_pkey
ALTER TABLE ONLY public.hay_transactions
    ADD CONSTRAINT hay_transactions_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.idempotency_keys idempotency_keys_pkey
ALTER TABLE ONLY public.idempotency_keys
    ADD CONSTRAINT idempotency_keys_pkey PRIMARY KEY (user_id, key);

-- CONSTRAINT: public.job_attempts job_attempts_job_id_attempt_key
ALTER TABLE ONLY public.job_attempts
    ADD CONSTRAINT job_attempts_job_id_attempt_key UNIQUE (job_id, attempt);

-- CONSTRAINT: public.job_attempts job_attempts_pkey
ALTER TABLE ONLY public.job_attempts
    ADD CONSTRAINT job_attempts_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.mem0_ingest_candidate_sources mem0_ingest_candidate_sources_candidate_id_source_message_i_key
ALTER TABLE ONLY public.mem0_ingest_candidate_sources
    ADD CONSTRAINT mem0_ingest_candidate_sources_candidate_id_source_message_i_key UNIQUE (candidate_id, source_message_id, evidence_start_utf8, evidence_end_utf8);

-- CONSTRAINT: public.mem0_ingest_candidates mem0_ingest_candidates_id_user_id_key
ALTER TABLE ONLY public.mem0_ingest_candidates
    ADD CONSTRAINT mem0_ingest_candidates_id_user_id_key UNIQUE (id, user_id);

-- CONSTRAINT: public.mem0_ingest_candidates mem0_ingest_candidates_pkey
ALTER TABLE ONLY public.mem0_ingest_candidates
    ADD CONSTRAINT mem0_ingest_candidates_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.mem0_ingest_candidates mem0_ingest_candidates_user_id_turn_seq_candidate_hash_sche_key
ALTER TABLE ONLY public.mem0_ingest_candidates
    ADD CONSTRAINT mem0_ingest_candidates_user_id_turn_seq_candidate_hash_sche_key UNIQUE (user_id, turn_seq, candidate_hash, schema_version, repair_generation);

-- CONSTRAINT: public.mem0_memory_registry mem0_memory_registry_id_user_id_key
ALTER TABLE ONLY public.mem0_memory_registry
    ADD CONSTRAINT mem0_memory_registry_id_user_id_key UNIQUE (id, user_id);

-- CONSTRAINT: public.mem0_memory_registry mem0_memory_registry_pkey
ALTER TABLE ONLY public.mem0_memory_registry
    ADD CONSTRAINT mem0_memory_registry_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.mem0_memory_registry mem0_memory_registry_user_id_provider_collection_version_pr_key
ALTER TABLE ONLY public.mem0_memory_registry
    ADD CONSTRAINT mem0_memory_registry_user_id_provider_collection_version_pr_key UNIQUE (user_id, provider, collection_version, provider_memory_id);

-- CONSTRAINT: public.mem0_memory_sources mem0_memory_sources_registry_id_source_message_id_evidence__key
ALTER TABLE ONLY public.mem0_memory_sources
    ADD CONSTRAINT mem0_memory_sources_registry_id_source_message_id_evidence__key UNIQUE (registry_id, source_message_id, evidence_start_utf8, evidence_end_utf8);

-- CONSTRAINT: public.memory_pipeline_states memory_pipeline_states_pkey
ALTER TABLE ONLY public.memory_pipeline_states
    ADD CONSTRAINT memory_pipeline_states_pkey PRIMARY KEY (user_id);

-- CONSTRAINT: public.messages messages_pkey
ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.moly_life_ments moly_life_ments_pkey
ALTER TABLE ONLY public.moly_life_ments
    ADD CONSTRAINT moly_life_ments_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.mood_entries mood_entries_pkey
ALTER TABLE ONLY public.mood_entries
    ADD CONSTRAINT mood_entries_pkey PRIMARY KEY (user_id, entry_date);

-- CONSTRAINT: public.order_items order_items_pkey
ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.orders orders_pkey
ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.payments payments_pkey
ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.payments payments_store_transaction_id_key
ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_store_transaction_id_key UNIQUE (store_transaction_id);

-- CONSTRAINT: public.privacy_ledger_events privacy_ledger_events_pkey
ALTER TABLE ONLY public.privacy_ledger_events
    ADD CONSTRAINT privacy_ledger_events_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.privacy_subject_barriers privacy_subject_barriers_pkey
ALTER TABLE ONLY public.privacy_subject_barriers
    ADD CONSTRAINT privacy_subject_barriers_pkey PRIMARY KEY (user_id);

-- CONSTRAINT: public.products products_app_store_product_id_key
ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_app_store_product_id_key UNIQUE (app_store_product_id);

-- CONSTRAINT: public.products products_id_slot_uq
ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_id_slot_uq UNIQUE (id, slot);

-- CONSTRAINT: public.products products_pkey
ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.products products_play_store_product_id_key
ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_play_store_product_id_key UNIQUE (play_store_product_id);

-- CONSTRAINT: public.profiles profiles_pkey
ALTER TABLE ONLY public.profiles
    ADD CONSTRAINT profiles_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.provider_backoffs provider_backoffs_pkey
ALTER TABLE ONLY public.provider_backoffs
    ADD CONSTRAINT provider_backoffs_pkey PRIMARY KEY (provider, model, lane);

-- CONSTRAINT: public.relationship_events relationship_events_pkey
ALTER TABLE ONLY public.relationship_events
    ADD CONSTRAINT relationship_events_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.relationship_events relationship_events_user_id_dedup_key_key
ALTER TABLE ONLY public.relationship_events
    ADD CONSTRAINT relationship_events_user_id_dedup_key_key UNIQUE (user_id, dedup_key);

-- CONSTRAINT: public.relationship_profile_renders relationship_profile_renders_pkey
ALTER TABLE ONLY public.relationship_profile_renders
    ADD CONSTRAINT relationship_profile_renders_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.relationship_profile_renders relationship_profile_renders_user_id_prompt_revision_profil_key
ALTER TABLE ONLY public.relationship_profile_renders
    ADD CONSTRAINT relationship_profile_renders_user_id_prompt_revision_profil_key UNIQUE (user_id, prompt_revision, profile_relationship_revision, locale, renderer_version);

-- CONSTRAINT: public.revenuecat_events revenuecat_events_pkey
ALTER TABLE ONLY public.revenuecat_events
    ADD CONSTRAINT revenuecat_events_pkey PRIMARY KEY (event_id);

-- CONSTRAINT: public.reward_ad_sessions reward_ad_sessions_pkey
ALTER TABLE ONLY public.reward_ad_sessions
    ADD CONSTRAINT reward_ad_sessions_pkey PRIMARY KEY (session_id);

-- CONSTRAINT: public.reward_ad_sessions reward_ad_sessions_ssv_transaction_id_key
ALTER TABLE ONLY public.reward_ad_sessions
    ADD CONSTRAINT reward_ad_sessions_ssv_transaction_id_key UNIQUE (ssv_transaction_id);

-- CONSTRAINT: public.routine_completions routine_completions_pkey
ALTER TABLE ONLY public.routine_completions
    ADD CONSTRAINT routine_completions_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.routine_completions routine_completions_routine_date_uq
ALTER TABLE ONLY public.routine_completions
    ADD CONSTRAINT routine_completions_routine_date_uq UNIQUE (routine_id, activity_date);

-- CONSTRAINT: public.routines routines_pkey
ALTER TABLE ONLY public.routines
    ADD CONSTRAINT routines_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.schema_migrations schema_migrations_pkey
ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (migration_name);

-- CONSTRAINT: public.shadow_prompt_traces shadow_prompt_traces_pkey
ALTER TABLE ONLY public.shadow_prompt_traces
    ADD CONSTRAINT shadow_prompt_traces_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.shadow_prompt_traces shadow_prompt_traces_turn_uniq
ALTER TABLE ONLY public.shadow_prompt_traces
    ADD CONSTRAINT shadow_prompt_traces_turn_uniq UNIQUE (user_id, turn_seq, assembler_version);

-- CONSTRAINT: public.subscription_hay_grants subscription_hay_grants_pkey
ALTER TABLE ONLY public.subscription_hay_grants
    ADD CONSTRAINT subscription_hay_grants_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.subscription_hay_grants subscription_hay_grants_user_plan_uq
ALTER TABLE ONLY public.subscription_hay_grants
    ADD CONSTRAINT subscription_hay_grants_user_plan_uq UNIQUE (user_id, plan);

-- CONSTRAINT: public.subscriptions subscriptions_original_transaction_id_key
ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_original_transaction_id_key UNIQUE (original_transaction_id);

-- CONSTRAINT: public.subscriptions subscriptions_pkey
ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.user_daily_stats user_daily_stats_pkey
ALTER TABLE ONLY public.user_daily_stats
    ADD CONSTRAINT user_daily_stats_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.user_daily_stats user_daily_stats_user_date_uq
ALTER TABLE ONLY public.user_daily_stats
    ADD CONSTRAINT user_daily_stats_user_date_uq UNIQUE (user_id, activity_date);

-- CONSTRAINT: public.user_devices user_devices_pkey
ALTER TABLE ONLY public.user_devices
    ADD CONSTRAINT user_devices_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.user_devices user_devices_push_token_key
ALTER TABLE ONLY public.user_devices
    ADD CONSTRAINT user_devices_push_token_key UNIQUE (push_token);

-- CONSTRAINT: public.user_interaction_contract_items user_interaction_contract_items_contract_id_item_key_key
ALTER TABLE ONLY public.user_interaction_contract_items
    ADD CONSTRAINT user_interaction_contract_items_contract_id_item_key_key UNIQUE (contract_id, item_key);

-- CONSTRAINT: public.user_interaction_contract_items user_interaction_contract_items_pkey
ALTER TABLE ONLY public.user_interaction_contract_items
    ADD CONSTRAINT user_interaction_contract_items_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.user_interaction_contracts user_interaction_contracts_id_user_id_key
ALTER TABLE ONLY public.user_interaction_contracts
    ADD CONSTRAINT user_interaction_contracts_id_user_id_key UNIQUE (id, user_id);

-- CONSTRAINT: public.user_interaction_contracts user_interaction_contracts_pkey
ALTER TABLE ONLY public.user_interaction_contracts
    ADD CONSTRAINT user_interaction_contracts_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.user_interaction_contracts user_interaction_contracts_user_id_locale_version_key
ALTER TABLE ONLY public.user_interaction_contracts
    ADD CONSTRAINT user_interaction_contracts_user_id_locale_version_key UNIQUE (user_id, locale, version);

-- CONSTRAINT: public.user_items user_items_pkey
ALTER TABLE ONLY public.user_items
    ADD CONSTRAINT user_items_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.user_items user_items_user_product_uq
ALTER TABLE ONLY public.user_items
    ADD CONSTRAINT user_items_user_product_uq UNIQUE (user_id, product_id);

-- CONSTRAINT: public.user_notification_settings user_notification_settings_pkey
ALTER TABLE ONLY public.user_notification_settings
    ADD CONSTRAINT user_notification_settings_pkey PRIMARY KEY (user_id, type);

-- CONSTRAINT: public.user_relationship_states user_relationship_states_pkey
ALTER TABLE ONLY public.user_relationship_states
    ADD CONSTRAINT user_relationship_states_pkey PRIMARY KEY (user_id);

-- CONSTRAINT: public.user_schedules user_schedules_pkey
ALTER TABLE ONLY public.user_schedules
    ADD CONSTRAINT user_schedules_pkey PRIMARY KEY (id);

-- CONSTRAINT: public.user_schedules user_schedules_user_kind_uniq
ALTER TABLE ONLY public.user_schedules
    ADD CONSTRAINT user_schedules_user_kind_uniq UNIQUE (user_id, kind);

-- CONSTRAINT: vecs.moly_memories_v2 moly_memories_v2_pkey
ALTER TABLE ONLY vecs.moly_memories_v2
    ADD CONSTRAINT moly_memories_v2_pkey PRIMARY KEY (id);

-- INDEX: public.ai_price_catalog_lookup_idx
CREATE INDEX ai_price_catalog_lookup_idx ON public.ai_price_catalog USING btree (provider, model, effective_from DESC);

-- INDEX: public.ai_usage_ledger_open_idx
CREATE INDEX ai_usage_ledger_open_idx ON public.ai_usage_ledger USING btree (status, started_at) WHERE (status = ANY (ARRAY['started'::text, 'unknown_usage'::text]));

-- INDEX: public.ai_usage_ledger_purpose_idx
CREATE INDEX ai_usage_ledger_purpose_idx ON public.ai_usage_ledger USING btree (purpose, started_at DESC);

-- INDEX: public.ai_usage_ledger_request_idx
CREATE INDEX ai_usage_ledger_request_idx ON public.ai_usage_ledger USING btree (provider, provider_request_id) WHERE (provider_request_id IS NOT NULL);

-- INDEX: public.ai_usage_ledger_started_idx
CREATE INDEX ai_usage_ledger_started_idx ON public.ai_usage_ledger USING btree (started_at);

-- INDEX: public.ai_usage_ledger_user_day_idx
CREATE INDEX ai_usage_ledger_user_day_idx ON public.ai_usage_ledger USING btree (user_id, activity_date, lane);

-- INDEX: public.async_jobs_claim_idx
CREATE INDEX async_jobs_claim_idx ON public.async_jobs USING btree (queue, priority, available_at, created_at) WHERE (state = 'ready'::text);

-- INDEX: public.async_jobs_finished_gc_idx
CREATE INDEX async_jobs_finished_gc_idx ON public.async_jobs USING btree (finished_at) WHERE (state = ANY (ARRAY['succeeded'::text, 'cancelled'::text]));

-- INDEX: public.async_jobs_reclaim_idx
CREATE INDEX async_jobs_reclaim_idx ON public.async_jobs USING btree (queue, lease_until) WHERE (state = 'running'::text);

-- INDEX: public.async_jobs_replay_of_idx
CREATE INDEX async_jobs_replay_of_idx ON public.async_jobs USING btree (replay_of) WHERE (replay_of IS NOT NULL);

-- INDEX: public.async_jobs_replay_operation_uq
CREATE UNIQUE INDEX async_jobs_replay_operation_uq ON public.async_jobs USING btree (replay_of, replay_operation_id) WHERE ((replay_of IS NOT NULL) AND (replay_operation_id IS NOT NULL));

-- INDEX: public.async_jobs_scrub_idx
CREATE INDEX async_jobs_scrub_idx ON public.async_jobs USING btree (payload_expires_at) WHERE ((payload_redacted_at IS NULL) AND (payload_expires_at IS NOT NULL));

-- INDEX: public.async_jobs_state_queue_idx
CREATE INDEX async_jobs_state_queue_idx ON public.async_jobs USING btree (state, queue);

-- INDEX: public.async_jobs_user_idx
CREATE INDEX async_jobs_user_idx ON public.async_jobs USING btree (user_id) WHERE (user_id IS NOT NULL);

-- INDEX: public.chat_active_turns_key_uq
CREATE UNIQUE INDEX chat_active_turns_key_uq ON public.chat_active_turns USING btree (user_id, idempotency_key);

-- INDEX: public.conversation_checkpoints_daily_uq
CREATE UNIQUE INDEX conversation_checkpoints_daily_uq ON public.conversation_checkpoints USING btree (user_id, activity_date_from) WHERE (kind = 'daily_digest'::text);

-- INDEX: public.conversation_checkpoints_latest_idx
CREATE INDEX conversation_checkpoints_latest_idx ON public.conversation_checkpoints USING btree (user_id, through_message_id DESC);

-- INDEX: public.conversation_checkpoints_live_idx
CREATE INDEX conversation_checkpoints_live_idx ON public.conversation_checkpoints USING btree (user_id, memory_generation, through_message_id DESC);

-- INDEX: public.conversation_checkpoints_published_window_uq
CREATE UNIQUE INDEX conversation_checkpoints_published_window_uq ON public.conversation_checkpoints USING btree (user_id, coverage_through_message_id) WHERE ((kind = 'window'::text) AND (publish_state = 'published'::text));

-- INDEX: public.diary_generation_results_user_preset_uq
CREATE UNIQUE INDEX diary_generation_results_user_preset_uq ON public.diary_generation_results USING btree (user_id, preset_ment_id) WHERE (status = 'preset'::text);

-- INDEX: public.diaries_one_daily_uq
CREATE UNIQUE INDEX diaries_one_daily_uq ON public.diaries USING btree (user_id, activity_date) WHERE ((kind = ANY (ARRAY['shared_day'::text, 'capi_day'::text])) AND (deleted_at IS NULL));

-- INDEX: public.diaries_one_welcome_uq
CREATE UNIQUE INDEX diaries_one_welcome_uq ON public.diaries USING btree (user_id) WHERE ((kind = 'welcome'::text) AND (deleted_at IS NULL));

-- INDEX: public.diaries_user_display_cursor_idx
CREATE INDEX diaries_user_display_cursor_idx ON public.diaries USING btree (user_id, display_date DESC, id DESC) WHERE ((record_status = 'published'::text) AND (deleted_at IS NULL));

-- INDEX: public.diaries_user_id_id_uq
CREATE UNIQUE INDEX diaries_user_id_id_uq ON public.diaries USING btree (user_id, id);

-- INDEX: public.diaries_user_published_idx
CREATE INDEX diaries_user_published_idx ON public.diaries USING btree (user_id, published_at);

-- INDEX: public.diary_claim_sources_user_msg_idx
CREATE INDEX diary_claim_sources_user_msg_idx ON public.diary_claim_sources USING btree (user_id, message_id);

-- INDEX: public.diary_recall_missing_embedding_idx
CREATE INDEX diary_recall_missing_embedding_idx ON public.diary_recall_documents USING btree (updated_at) WHERE (embedding IS NULL);

-- INDEX: public.feedback_user_idx
CREATE INDEX feedback_user_idx ON public.feedback USING btree (user_id);

-- INDEX: public.fortune_ad_sessions_lookup_idx
CREATE INDEX fortune_ad_sessions_lookup_idx ON public.fortune_ad_sessions USING btree (user_id, fortune_date, verified);

-- INDEX: public.fortune_ad_sessions_retention_idx
CREATE INDEX fortune_ad_sessions_retention_idx ON public.fortune_ad_sessions USING btree (expires_at, session_id);

-- INDEX: public.greetings_committed_message_idx
CREATE INDEX greetings_committed_message_idx ON public.greetings USING btree (committed_message_id) WHERE (committed_message_id IS NOT NULL);

-- INDEX: public.hay_transactions_order_idx
CREATE INDEX hay_transactions_order_idx ON public.hay_transactions USING btree (order_id);

-- INDEX: public.hay_transactions_user_created_idx
CREATE INDEX hay_transactions_user_created_idx ON public.hay_transactions USING btree (user_id, created_at DESC);

-- INDEX: public.hay_transactions_user_id_idx
CREATE INDEX hay_transactions_user_id_idx ON public.hay_transactions USING btree (user_id, id);

-- INDEX: public.idempotency_keys_dedupe_gc_idx
CREATE INDEX idempotency_keys_dedupe_gc_idx ON public.idempotency_keys USING btree (dedupe_expires_at) WHERE (dedupe_expires_at IS NOT NULL);

-- INDEX: public.idempotency_keys_scrub_idx
CREATE INDEX idempotency_keys_scrub_idx ON public.idempotency_keys USING btree (response_expires_at) WHERE (response IS NOT NULL);

-- INDEX: public.idempotency_reply_idx
CREATE INDEX idempotency_reply_idx ON public.idempotency_keys USING btree (user_id, reply_message_id) WHERE (reply_message_id IS NOT NULL);

-- INDEX: public.job_attempts_open_idx
CREATE INDEX job_attempts_open_idx ON public.job_attempts USING btree (job_id) WHERE (outcome IS NULL);

-- INDEX: public.job_attempts_queue_idx
CREATE INDEX job_attempts_queue_idx ON public.job_attempts USING btree (queue, started_at DESC);

-- INDEX: public.mem0_ingest_candidate_sources_user_msg_idx
CREATE INDEX mem0_ingest_candidate_sources_user_msg_idx ON public.mem0_ingest_candidate_sources USING btree (user_id, source_message_id);

-- INDEX: public.mem0_ingest_candidates_open_idx
CREATE INDEX mem0_ingest_candidates_open_idx ON public.mem0_ingest_candidates USING btree (user_id, turn_seq) WHERE (status = 'planned'::text);

-- INDEX: public.mem0_memory_registry_category_idx
CREATE INDEX mem0_memory_registry_category_idx ON public.mem0_memory_registry USING btree (user_id, category) WHERE (semantic_status = ANY (ARRAY['active'::text, 'ambiguous'::text]));

-- INDEX: public.mem0_memory_registry_conflict_idx
CREATE INDEX mem0_memory_registry_conflict_idx ON public.mem0_memory_registry USING btree (conflict_group_id) WHERE (conflict_group_id IS NOT NULL);

-- INDEX: public.mem0_memory_registry_delete_backlog_idx
CREATE INDEX mem0_memory_registry_delete_backlog_idx ON public.mem0_memory_registry USING btree (provider_delete_state, updated_at) WHERE (provider_delete_state = 'pending'::text);

-- INDEX: public.mem0_memory_registry_delete_scan_idx
CREATE INDEX mem0_memory_registry_delete_scan_idx ON public.mem0_memory_registry USING btree (provider_delete_state) WHERE (provider_delete_state = ANY (ARRAY['pending'::text, 'failed'::text]));

-- INDEX: public.mem0_memory_registry_reconsolidate_idx
CREATE INDEX mem0_memory_registry_reconsolidate_idx ON public.mem0_memory_registry USING btree (user_id, last_reconsolidated_at NULLS FIRST) WHERE (semantic_status = ANY (ARRAY['active'::text, 'ambiguous'::text]));

-- INDEX: public.mem0_memory_registry_searchable_idx
CREATE INDEX mem0_memory_registry_searchable_idx ON public.mem0_memory_registry USING btree (user_id, semantic_status, source_turn_seq) WHERE (semantic_status = ANY (ARRAY['active'::text, 'ambiguous'::text]));

-- INDEX: public.mem0_memory_sources_date_idx
CREATE INDEX mem0_memory_sources_date_idx ON public.mem0_memory_sources USING btree (user_id, source_activity_date);

-- INDEX: public.mem0_memory_sources_user_msg_idx
CREATE INDEX mem0_memory_sources_user_msg_idx ON public.mem0_memory_sources USING btree (user_id, source_message_id);

-- INDEX: public.memory_pipeline_states_lag_idx
CREATE INDEX memory_pipeline_states_lag_idx ON public.memory_pipeline_states USING btree (user_id) WHERE (consolidated_through_turn_seq < source_through_turn_seq);

-- INDEX: public.memory_pipeline_states_mode_idx
CREATE INDEX memory_pipeline_states_mode_idx ON public.memory_pipeline_states USING btree (mode, bootstrap_status);

-- INDEX: public.messages_fortune_context_root_idx
CREATE INDEX messages_fortune_context_root_idx ON public.messages USING btree (user_id, id DESC) WHERE ((sender = 'user'::text) AND (kind = 'fortune_context_root'::text));

-- INDEX: public.messages_user_actdate_idx
CREATE INDEX messages_user_actdate_idx ON public.messages USING btree (user_id, activity_date);

-- INDEX: public.messages_user_id_id_sender_uq
CREATE UNIQUE INDEX messages_user_id_id_sender_uq ON public.messages USING btree (user_id, id, sender);

-- INDEX: public.messages_user_id_id_uq
CREATE UNIQUE INDEX messages_user_id_id_uq ON public.messages USING btree (user_id, id);

-- INDEX: public.messages_user_turn_position_uq
CREATE UNIQUE INDEX messages_user_turn_position_uq ON public.messages USING btree (user_id, turn_seq, turn_position) WHERE (turn_seq IS NOT NULL);

-- INDEX: public.moly_life_ments_week_sequence_uq
CREATE UNIQUE INDEX moly_life_ments_week_sequence_uq ON public.moly_life_ments USING btree (week_start_date, sequence_no) WHERE (week_start_date IS NOT NULL);

-- INDEX: public.moly_life_ments_diary_date_uq
CREATE UNIQUE INDEX moly_life_ments_diary_date_uq ON public.moly_life_ments USING btree (diary_date) WHERE (diary_date IS NOT NULL);

-- INDEX: public.order_items_order_idx
CREATE INDEX order_items_order_idx ON public.order_items USING btree (order_id);

-- INDEX: public.order_items_product_idx
CREATE INDEX order_items_product_idx ON public.order_items USING btree (product_id);

-- INDEX: public.orders_user_created_idx
CREATE INDEX orders_user_created_idx ON public.orders USING btree (user_id, created_at DESC);

-- INDEX: public.payments_order_idx
CREATE INDEX payments_order_idx ON public.payments USING btree (order_id);

-- INDEX: public.payments_store_idx
CREATE INDEX payments_store_idx ON public.payments USING btree (store);

-- INDEX: public.payments_subscription_idx
CREATE INDEX payments_subscription_idx ON public.payments USING btree (subscription_id);

-- INDEX: public.payments_user_idx
CREATE INDEX payments_user_idx ON public.payments USING btree (user_id);

-- INDEX: public.privacy_ledger_user_idx
CREATE INDEX privacy_ledger_user_idx ON public.privacy_ledger_events USING btree (user_id, id);

-- INDEX: public.privacy_subject_barriers_state_idx
CREATE INDEX privacy_subject_barriers_state_idx ON public.privacy_subject_barriers USING btree (state);

-- INDEX: public.products_public_id_uq
CREATE UNIQUE INDEX products_public_id_uq ON public.products USING btree (public_id) WHERE (public_id IS NOT NULL);

-- INDEX: public.relationship_events_replay_idx
CREATE INDEX relationship_events_replay_idx ON public.relationship_events USING btree (user_id, activity_date, id);

-- INDEX: public.relationship_profile_renders_lookup_idx
CREATE INDEX relationship_profile_renders_lookup_idx ON public.relationship_profile_renders USING btree (user_id, locale, prompt_revision DESC);

-- INDEX: public.revenuecat_events_status_idx
CREATE INDEX revenuecat_events_status_idx ON public.revenuecat_events USING btree (status, received_at);

-- INDEX: public.revenuecat_events_status_next_attempt_idx
CREATE INDEX revenuecat_events_status_next_attempt_idx ON public.revenuecat_events USING btree (status, next_attempt_at);

-- INDEX: public.reward_ad_sessions_expiry_idx
CREATE INDEX reward_ad_sessions_expiry_idx ON public.reward_ad_sessions USING btree (expires_at, session_id);

-- INDEX: public.reward_ad_sessions_user_idx
CREATE INDEX reward_ad_sessions_user_idx ON public.reward_ad_sessions USING btree (user_id);

-- INDEX: public.routine_completions_routine_idx
CREATE INDEX routine_completions_routine_idx ON public.routine_completions USING btree (routine_id);

-- INDEX: public.routine_completions_user_actdate_idx
CREATE INDEX routine_completions_user_actdate_idx ON public.routine_completions USING btree (user_id, activity_date);

-- INDEX: public.routine_completions_user_idx
CREATE INDEX routine_completions_user_idx ON public.routine_completions USING btree (user_id);

-- INDEX: public.routines_user_id_id_uq
CREATE UNIQUE INDEX routines_user_id_id_uq ON public.routines USING btree (user_id, id);

-- INDEX: public.routines_user_idx
CREATE INDEX routines_user_idx ON public.routines USING btree (user_id);

-- INDEX: public.shadow_prompt_traces_user_idx
CREATE INDEX shadow_prompt_traces_user_idx ON public.shadow_prompt_traces USING btree (user_id, turn_seq);

-- INDEX: public.subscriptions_user_idx
CREATE INDEX subscriptions_user_idx ON public.subscriptions USING btree (user_id);

-- INDEX: public.user_devices_user_idx
CREATE INDEX user_devices_user_idx ON public.user_devices USING btree (user_id);

-- INDEX: public.user_interaction_contract_items_active_idx
CREATE INDEX user_interaction_contract_items_active_idx ON public.user_interaction_contract_items USING btree (user_id, section) WHERE (status = 'active'::text);

-- INDEX: public.user_interaction_contracts_published_uq
CREATE UNIQUE INDEX user_interaction_contracts_published_uq ON public.user_interaction_contracts USING btree (user_id, locale) WHERE (status = 'published'::text);

-- INDEX: public.user_items_order_idx
CREATE INDEX user_items_order_idx ON public.user_items USING btree (order_id);

-- INDEX: public.user_items_product_idx
CREATE INDEX user_items_product_idx ON public.user_items USING btree (product_id);

-- INDEX: public.user_items_user_equipped_slot_uq
CREATE UNIQUE INDEX user_items_user_equipped_slot_uq ON public.user_items USING btree (user_id, equipped_slot) WHERE (equipped_slot IS NOT NULL);

-- INDEX: public.user_items_user_idx
CREATE INDEX user_items_user_idx ON public.user_items USING btree (user_id);

-- INDEX: public.user_schedules_due_idx
CREATE INDEX user_schedules_due_idx ON public.user_schedules USING btree (kind, next_due_at);

-- INDEX: vecs.moly_memories_v2_user_idx
CREATE INDEX moly_memories_v2_user_idx ON vecs.moly_memories_v2 USING btree (((metadata ->> 'user_id'::text)));

-- TRIGGER: public.chat_contexts chat_contexts_normalized_snapshot_guard
CREATE TRIGGER chat_contexts_normalized_snapshot_guard BEFORE INSERT OR UPDATE ON public.chat_contexts FOR EACH ROW EXECUTE FUNCTION public.guard_normalized_memory_snapshot();

-- TRIGGER: public.orders orders_set_updated_at
CREATE TRIGGER orders_set_updated_at BEFORE UPDATE ON public.orders FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- TRIGGER: public.profiles profiles_create_privacy_barrier
CREATE TRIGGER profiles_create_privacy_barrier AFTER INSERT ON public.profiles FOR EACH ROW EXECUTE FUNCTION public.create_privacy_barrier_for_profile();

-- TRIGGER: public.profiles profiles_set_updated_at
CREATE TRIGGER profiles_set_updated_at BEFORE UPDATE ON public.profiles FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- TRIGGER: public.routines routines_set_updated_at
CREATE TRIGGER routines_set_updated_at BEFORE UPDATE ON public.routines FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- TRIGGER: public.subscriptions subscriptions_set_updated_at
CREATE TRIGGER subscriptions_set_updated_at BEFORE UPDATE ON public.subscriptions FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- TRIGGER: public.profiles trg_normalize_profile_language
CREATE TRIGGER trg_normalize_profile_language BEFORE INSERT OR UPDATE OF language ON public.profiles FOR EACH ROW EXECUTE FUNCTION public.normalize_profile_language();

-- FK CONSTRAINT: public.ai_usage_ledger ai_usage_ledger_user_id_fkey
ALTER TABLE ONLY public.ai_usage_ledger
    ADD CONSTRAINT ai_usage_ledger_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE SET NULL;

-- FK CONSTRAINT: public.async_jobs async_jobs_replay_of_fkey
ALTER TABLE ONLY public.async_jobs
    ADD CONSTRAINT async_jobs_replay_of_fkey FOREIGN KEY (replay_of) REFERENCES public.async_jobs(id);

-- FK CONSTRAINT: public.async_jobs async_jobs_user_id_fkey
ALTER TABLE ONLY public.async_jobs
    ADD CONSTRAINT async_jobs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.chat_active_turns chat_active_turns_user_id_fkey
ALTER TABLE ONLY public.chat_active_turns
    ADD CONSTRAINT chat_active_turns_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.chat_contexts chat_contexts_user_id_fkey
ALTER TABLE ONLY public.chat_contexts
    ADD CONSTRAINT chat_contexts_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.chat_response_references chat_response_references_user_id_diary_id_fkey
ALTER TABLE ONLY public.chat_response_references
    ADD CONSTRAINT chat_response_references_user_id_diary_id_fkey FOREIGN KEY (user_id, diary_id) REFERENCES public.diaries(user_id, id) ON DELETE RESTRICT;

-- FK CONSTRAINT: public.chat_response_references chat_response_references_user_id_reply_message_id_fkey
ALTER TABLE ONLY public.chat_response_references
    ADD CONSTRAINT chat_response_references_user_id_reply_message_id_fkey FOREIGN KEY (user_id, reply_message_id) REFERENCES public.messages(user_id, id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.conversation_checkpoints conversation_checkpoints_user_id_fkey
ALTER TABLE ONLY public.conversation_checkpoints
    ADD CONSTRAINT conversation_checkpoints_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.conversation_checkpoints conversation_checkpoints_user_message_fk
ALTER TABLE ONLY public.conversation_checkpoints
    ADD CONSTRAINT conversation_checkpoints_user_message_fk FOREIGN KEY (user_id, through_message_id) REFERENCES public.messages(user_id, id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.conversation_focus conversation_focus_user_id_fkey
ALTER TABLE ONLY public.conversation_focus
    ADD CONSTRAINT conversation_focus_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.daily_fortunes daily_fortunes_user_id_fkey
ALTER TABLE ONLY public.daily_fortunes
    ADD CONSTRAINT daily_fortunes_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.fortune_profiles(user_id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.diaries diaries_preset_ment_id_fkey
ALTER TABLE ONLY public.diaries
    ADD CONSTRAINT diaries_preset_ment_id_fkey FOREIGN KEY (preset_ment_id) REFERENCES public.moly_life_ments(id) ON DELETE SET NULL;

-- FK CONSTRAINT: public.diaries diaries_user_id_fkey
ALTER TABLE ONLY public.diaries
    ADD CONSTRAINT diaries_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.diary_claim_sources diary_claim_sources_user_id_diary_id_fkey
ALTER TABLE ONLY public.diary_claim_sources
    ADD CONSTRAINT diary_claim_sources_user_id_diary_id_fkey FOREIGN KEY (user_id, diary_id) REFERENCES public.diaries(user_id, id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.diary_claim_sources diary_claim_sources_user_id_message_id_fkey
ALTER TABLE ONLY public.diary_claim_sources
    ADD CONSTRAINT diary_claim_sources_user_id_message_id_fkey FOREIGN KEY (user_id, message_id) REFERENCES public.messages(user_id, id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.diary_generation_results diary_generation_results_preset_ment_id_fkey
ALTER TABLE ONLY public.diary_generation_results
    ADD CONSTRAINT diary_generation_results_preset_ment_id_fkey FOREIGN KEY (preset_ment_id) REFERENCES public.moly_life_ments(id) ON DELETE RESTRICT;

-- FK CONSTRAINT: public.diary_generation_results diary_generation_results_user_id_fkey
ALTER TABLE ONLY public.diary_generation_results
    ADD CONSTRAINT diary_generation_results_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.diary_recall_documents diary_recall_documents_user_id_diary_id_fkey
ALTER TABLE ONLY public.diary_recall_documents
    ADD CONSTRAINT diary_recall_documents_user_id_diary_id_fkey FOREIGN KEY (user_id, diary_id) REFERENCES public.diaries(user_id, id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.feedback feedback_user_id_fkey
ALTER TABLE ONLY public.feedback
    ADD CONSTRAINT feedback_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.fortune_ad_sessions fortune_ad_sessions_user_id_fkey
ALTER TABLE ONLY public.fortune_ad_sessions
    ADD CONSTRAINT fortune_ad_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.fortune_profiles(user_id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.fortune_profiles fortune_profiles_user_id_fkey
ALTER TABLE ONLY public.fortune_profiles
    ADD CONSTRAINT fortune_profiles_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.greetings greetings_committed_message_id_fkey
ALTER TABLE ONLY public.greetings
    ADD CONSTRAINT greetings_committed_message_id_fkey FOREIGN KEY (committed_message_id) REFERENCES public.messages(id) ON DELETE SET NULL;

-- FK CONSTRAINT: public.greetings greetings_user_id_fkey
ALTER TABLE ONLY public.greetings
    ADD CONSTRAINT greetings_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.hay_transactions hay_transactions_order_id_fkey
ALTER TABLE ONLY public.hay_transactions
    ADD CONSTRAINT hay_transactions_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id) ON DELETE SET NULL;

-- FK CONSTRAINT: public.hay_transactions hay_transactions_user_id_fkey
ALTER TABLE ONLY public.hay_transactions
    ADD CONSTRAINT hay_transactions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.idempotency_keys idempotency_keys_user_id_fkey
ALTER TABLE ONLY public.idempotency_keys
    ADD CONSTRAINT idempotency_keys_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.idempotency_keys idempotency_reply_message_fk
ALTER TABLE ONLY public.idempotency_keys
    ADD CONSTRAINT idempotency_reply_message_fk FOREIGN KEY (user_id, reply_message_id) REFERENCES public.messages(user_id, id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.job_attempts job_attempts_job_id_fkey
ALTER TABLE ONLY public.job_attempts
    ADD CONSTRAINT job_attempts_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.async_jobs(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.mem0_ingest_candidate_sources mem0_ingest_candidate_sources_candidate_id_user_id_fkey
ALTER TABLE ONLY public.mem0_ingest_candidate_sources
    ADD CONSTRAINT mem0_ingest_candidate_sources_candidate_id_user_id_fkey FOREIGN KEY (candidate_id, user_id) REFERENCES public.mem0_ingest_candidates(id, user_id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.mem0_ingest_candidate_sources mem0_ingest_candidate_sources_user_id_source_message_id_so_fkey
ALTER TABLE ONLY public.mem0_ingest_candidate_sources
    ADD CONSTRAINT mem0_ingest_candidate_sources_user_id_source_message_id_so_fkey FOREIGN KEY (user_id, source_message_id, source_sender) REFERENCES public.messages(user_id, id, sender) ON DELETE CASCADE;

-- FK CONSTRAINT: public.mem0_ingest_candidates mem0_ingest_candidates_user_id_fkey
ALTER TABLE ONLY public.mem0_ingest_candidates
    ADD CONSTRAINT mem0_ingest_candidates_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.mem0_memory_registry mem0_memory_registry_user_id_fkey
ALTER TABLE ONLY public.mem0_memory_registry
    ADD CONSTRAINT mem0_memory_registry_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.mem0_memory_sources mem0_memory_sources_registry_id_user_id_fkey
ALTER TABLE ONLY public.mem0_memory_sources
    ADD CONSTRAINT mem0_memory_sources_registry_id_user_id_fkey FOREIGN KEY (registry_id, user_id) REFERENCES public.mem0_memory_registry(id, user_id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.mem0_memory_sources mem0_memory_sources_user_id_source_message_id_source_sende_fkey
ALTER TABLE ONLY public.mem0_memory_sources
    ADD CONSTRAINT mem0_memory_sources_user_id_source_message_id_source_sende_fkey FOREIGN KEY (user_id, source_message_id, source_sender) REFERENCES public.messages(user_id, id, sender) ON DELETE CASCADE;

-- FK CONSTRAINT: public.memory_pipeline_states memory_pipeline_states_user_id_fkey
ALTER TABLE ONLY public.memory_pipeline_states
    ADD CONSTRAINT memory_pipeline_states_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.messages messages_user_id_fkey
ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.mood_entries mood_entries_user_id_fkey
ALTER TABLE ONLY public.mood_entries
    ADD CONSTRAINT mood_entries_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.order_items order_items_order_id_fkey
ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.order_items order_items_product_id_fkey
ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE RESTRICT;

-- FK CONSTRAINT: public.orders orders_user_id_fkey
ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.payments payments_order_id_fkey
ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.payments payments_subscription_id_fkey
ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.payments payments_user_id_fkey
ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.profiles profiles_id_fkey
ALTER TABLE ONLY public.profiles
    ADD CONSTRAINT profiles_id_fkey FOREIGN KEY (id) REFERENCES auth.users(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.relationship_events relationship_events_user_id_fkey
ALTER TABLE ONLY public.relationship_events
    ADD CONSTRAINT relationship_events_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.relationship_profile_renders relationship_profile_renders_user_id_fkey
ALTER TABLE ONLY public.relationship_profile_renders
    ADD CONSTRAINT relationship_profile_renders_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.reward_ad_sessions reward_ad_sessions_user_id_fkey
ALTER TABLE ONLY public.reward_ad_sessions
    ADD CONSTRAINT reward_ad_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.routine_completions routine_completions_routine_id_fkey
ALTER TABLE ONLY public.routine_completions
    ADD CONSTRAINT routine_completions_routine_id_fkey FOREIGN KEY (routine_id) REFERENCES public.routines(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.routine_completions routine_completions_user_id_fkey
ALTER TABLE ONLY public.routine_completions
    ADD CONSTRAINT routine_completions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.routine_completions routine_completions_user_routine_fk
ALTER TABLE ONLY public.routine_completions
    ADD CONSTRAINT routine_completions_user_routine_fk FOREIGN KEY (user_id, routine_id) REFERENCES public.routines(user_id, id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.routines routines_user_id_fkey
ALTER TABLE ONLY public.routines
    ADD CONSTRAINT routines_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.shadow_prompt_traces shadow_prompt_traces_user_id_fkey
ALTER TABLE ONLY public.shadow_prompt_traces
    ADD CONSTRAINT shadow_prompt_traces_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.subscription_hay_grants subscription_hay_grants_clawback_hay_transaction_id_fkey
ALTER TABLE ONLY public.subscription_hay_grants
    ADD CONSTRAINT subscription_hay_grants_clawback_hay_transaction_id_fkey FOREIGN KEY (clawback_hay_transaction_id) REFERENCES public.hay_transactions(id) ON DELETE SET NULL;

-- FK CONSTRAINT: public.subscription_hay_grants subscription_hay_grants_hay_transaction_id_fkey
ALTER TABLE ONLY public.subscription_hay_grants
    ADD CONSTRAINT subscription_hay_grants_hay_transaction_id_fkey FOREIGN KEY (hay_transaction_id) REFERENCES public.hay_transactions(id) ON DELETE SET NULL;

-- FK CONSTRAINT: public.subscription_hay_grants subscription_hay_grants_user_id_fkey
ALTER TABLE ONLY public.subscription_hay_grants
    ADD CONSTRAINT subscription_hay_grants_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.subscriptions subscriptions_user_id_fkey
ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.user_daily_stats user_daily_stats_user_id_fkey
ALTER TABLE ONLY public.user_daily_stats
    ADD CONSTRAINT user_daily_stats_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.user_devices user_devices_user_id_fkey
ALTER TABLE ONLY public.user_devices
    ADD CONSTRAINT user_devices_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.user_interaction_contract_items user_interaction_contract_items_contract_id_user_id_fkey
ALTER TABLE ONLY public.user_interaction_contract_items
    ADD CONSTRAINT user_interaction_contract_items_contract_id_user_id_fkey FOREIGN KEY (contract_id, user_id) REFERENCES public.user_interaction_contracts(id, user_id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.user_interaction_contract_items user_interaction_contract_items_user_id_source_message_id_fkey
ALTER TABLE ONLY public.user_interaction_contract_items
    ADD CONSTRAINT user_interaction_contract_items_user_id_source_message_id_fkey FOREIGN KEY (user_id, source_message_id) REFERENCES public.messages(user_id, id) ON DELETE SET NULL (source_message_id);

-- FK CONSTRAINT: public.user_interaction_contracts user_interaction_contracts_user_id_fkey
ALTER TABLE ONLY public.user_interaction_contracts
    ADD CONSTRAINT user_interaction_contracts_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.user_items user_items_order_id_fkey
ALTER TABLE ONLY public.user_items
    ADD CONSTRAINT user_items_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id) ON DELETE SET NULL;

-- FK CONSTRAINT: public.user_items user_items_product_id_fkey
ALTER TABLE ONLY public.user_items
    ADD CONSTRAINT user_items_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.user_items user_items_product_slot_fk
ALTER TABLE ONLY public.user_items
    ADD CONSTRAINT user_items_product_slot_fk FOREIGN KEY (product_id, equipped_slot) REFERENCES public.products(id, slot);

-- FK CONSTRAINT: public.user_items user_items_user_id_fkey
ALTER TABLE ONLY public.user_items
    ADD CONSTRAINT user_items_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.user_notification_settings user_notification_settings_user_id_fkey
ALTER TABLE ONLY public.user_notification_settings
    ADD CONSTRAINT user_notification_settings_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.user_relationship_states user_relationship_states_user_id_fkey
ALTER TABLE ONLY public.user_relationship_states
    ADD CONSTRAINT user_relationship_states_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- FK CONSTRAINT: public.user_schedules user_schedules_user_id_fkey
ALTER TABLE ONLY public.user_schedules
    ADD CONSTRAINT user_schedules_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- ROW SECURITY: public.ai_price_catalog
ALTER TABLE public.ai_price_catalog ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.ai_usage_daily_rollup
ALTER TABLE public.ai_usage_daily_rollup ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.ai_usage_ledger
ALTER TABLE public.ai_usage_ledger ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.app_config
ALTER TABLE public.app_config ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.async_jobs
ALTER TABLE public.async_jobs ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.chat_active_turns
ALTER TABLE public.chat_active_turns ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.chat_contexts
ALTER TABLE public.chat_contexts ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.chat_response_references
ALTER TABLE public.chat_response_references ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.conversation_checkpoints
ALTER TABLE public.conversation_checkpoints ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.conversation_focus
ALTER TABLE public.conversation_focus ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.daily_fortunes
ALTER TABLE public.daily_fortunes ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.diaries
ALTER TABLE public.diaries ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.diary_claim_sources
ALTER TABLE public.diary_claim_sources ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.diary_gen_claims
ALTER TABLE public.diary_gen_claims ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.diary_generation_results
ALTER TABLE public.diary_generation_results ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.diary_recall_documents
ALTER TABLE public.diary_recall_documents ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.feedback
ALTER TABLE public.feedback ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.fortune_ad_sessions
ALTER TABLE public.fortune_ad_sessions ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.fortune_profiles
ALTER TABLE public.fortune_profiles ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.greetings
ALTER TABLE public.greetings ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.hay_transactions
ALTER TABLE public.hay_transactions ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.idempotency_keys
ALTER TABLE public.idempotency_keys ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.job_attempts
ALTER TABLE public.job_attempts ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.mem0_ingest_candidate_sources
ALTER TABLE public.mem0_ingest_candidate_sources ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.mem0_ingest_candidates
ALTER TABLE public.mem0_ingest_candidates ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.mem0_memory_registry
ALTER TABLE public.mem0_memory_registry ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.mem0_memory_sources
ALTER TABLE public.mem0_memory_sources ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.memory_pipeline_states
ALTER TABLE public.memory_pipeline_states ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.messages
ALTER TABLE public.messages ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.moly_life_ments
ALTER TABLE public.moly_life_ments ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.mood_entries
ALTER TABLE public.mood_entries ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.order_items
ALTER TABLE public.order_items ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.orders
ALTER TABLE public.orders ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.payments
ALTER TABLE public.payments ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.privacy_ledger_events
ALTER TABLE public.privacy_ledger_events ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.privacy_subject_barriers
ALTER TABLE public.privacy_subject_barriers ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.products
ALTER TABLE public.products ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.profiles
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.provider_backoffs
ALTER TABLE public.provider_backoffs ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.relationship_events
ALTER TABLE public.relationship_events ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.relationship_profile_renders
ALTER TABLE public.relationship_profile_renders ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.revenuecat_events
ALTER TABLE public.revenuecat_events ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.reward_ad_sessions
ALTER TABLE public.reward_ad_sessions ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.routine_completions
ALTER TABLE public.routine_completions ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.routines
ALTER TABLE public.routines ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.schema_migrations
ALTER TABLE public.schema_migrations ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.shadow_prompt_traces
ALTER TABLE public.shadow_prompt_traces ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.subscription_hay_grants
ALTER TABLE public.subscription_hay_grants ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.subscriptions
ALTER TABLE public.subscriptions ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.user_daily_stats
ALTER TABLE public.user_daily_stats ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.user_devices
ALTER TABLE public.user_devices ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.user_interaction_contract_items
ALTER TABLE public.user_interaction_contract_items ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.user_interaction_contracts
ALTER TABLE public.user_interaction_contracts ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.user_items
ALTER TABLE public.user_items ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.user_notification_settings
ALTER TABLE public.user_notification_settings ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.user_relationship_states
ALTER TABLE public.user_relationship_states ENABLE ROW LEVEL SECURITY;

-- ROW SECURITY: public.user_schedules
ALTER TABLE public.user_schedules ENABLE ROW LEVEL SECURITY;

-- ACL: public.FUNCTION bootstrap_user(p_user_id uuid, p_created_at timestamp with time zone)
REVOKE ALL ON FUNCTION public.bootstrap_user(p_user_id uuid, p_created_at timestamp with time zone) FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public.bootstrap_user(p_user_id uuid, p_created_at timestamp with time zone) FROM PUBLIC;
GRANT ALL ON FUNCTION public.bootstrap_user(p_user_id uuid, p_created_at timestamp with time zone) TO service_role;

-- ACL: public.FUNCTION create_privacy_barrier_for_profile()
REVOKE ALL ON FUNCTION public.create_privacy_barrier_for_profile() FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON FUNCTION public.create_privacy_barrier_for_profile() TO anon;
GRANT ALL ON FUNCTION public.create_privacy_barrier_for_profile() TO authenticated;
GRANT ALL ON FUNCTION public.create_privacy_barrier_for_profile() TO service_role;
GRANT EXECUTE ON FUNCTION public.create_privacy_barrier_for_profile() TO PUBLIC;

-- ACL: public.FUNCTION delete_user_memories(p_user_id uuid)
REVOKE ALL ON FUNCTION public.delete_user_memories(p_user_id uuid) FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public.delete_user_memories(p_user_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.delete_user_memories(p_user_id uuid) TO service_role;

-- ACL: public.FUNCTION guard_normalized_memory_snapshot()
REVOKE ALL ON FUNCTION public.guard_normalized_memory_snapshot() FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON FUNCTION public.guard_normalized_memory_snapshot() TO anon;
GRANT ALL ON FUNCTION public.guard_normalized_memory_snapshot() TO authenticated;
GRANT ALL ON FUNCTION public.guard_normalized_memory_snapshot() TO service_role;
GRANT EXECUTE ON FUNCTION public.guard_normalized_memory_snapshot() TO PUBLIC;

-- ACL: public.FUNCTION handle_new_user()
REVOKE ALL ON FUNCTION public.handle_new_user() FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON FUNCTION public.handle_new_user() TO anon;
GRANT ALL ON FUNCTION public.handle_new_user() TO authenticated;
GRANT ALL ON FUNCTION public.handle_new_user() TO service_role;
GRANT EXECUTE ON FUNCTION public.handle_new_user() TO PUBLIC;

-- ACL: public.FUNCTION normalize_content_language(tag text)
REVOKE ALL ON FUNCTION public.normalize_content_language(tag text) FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON FUNCTION public.normalize_content_language(tag text) TO anon;
GRANT ALL ON FUNCTION public.normalize_content_language(tag text) TO authenticated;
GRANT ALL ON FUNCTION public.normalize_content_language(tag text) TO service_role;
GRANT EXECUTE ON FUNCTION public.normalize_content_language(tag text) TO PUBLIC;

-- ACL: public.FUNCTION normalize_profile_language()
REVOKE ALL ON FUNCTION public.normalize_profile_language() FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON FUNCTION public.normalize_profile_language() TO anon;
GRANT ALL ON FUNCTION public.normalize_profile_language() TO authenticated;
GRANT ALL ON FUNCTION public.normalize_profile_language() TO service_role;
GRANT EXECUTE ON FUNCTION public.normalize_profile_language() TO PUBLIC;

-- ACL: public.FUNCTION set_updated_at()
REVOKE ALL ON FUNCTION public.set_updated_at() FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON FUNCTION public.set_updated_at() TO anon;
GRANT ALL ON FUNCTION public.set_updated_at() TO authenticated;
GRANT ALL ON FUNCTION public.set_updated_at() TO service_role;
GRANT EXECUTE ON FUNCTION public.set_updated_at() TO PUBLIC;

-- ACL: public.TABLE ai_price_catalog
REVOKE ALL ON TABLE public.ai_price_catalog FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.ai_price_catalog TO anon;
GRANT ALL ON TABLE public.ai_price_catalog TO authenticated;
GRANT ALL ON TABLE public.ai_price_catalog TO service_role;

-- ACL: public.TABLE ai_usage_daily_rollup
REVOKE ALL ON TABLE public.ai_usage_daily_rollup FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.ai_usage_daily_rollup TO anon;
GRANT ALL ON TABLE public.ai_usage_daily_rollup TO authenticated;
GRANT ALL ON TABLE public.ai_usage_daily_rollup TO service_role;

-- ACL: public.TABLE ai_usage_ledger
REVOKE ALL ON TABLE public.ai_usage_ledger FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.ai_usage_ledger TO anon;
GRANT ALL ON TABLE public.ai_usage_ledger TO authenticated;
GRANT ALL ON TABLE public.ai_usage_ledger TO service_role;

-- ACL: public.TABLE app_config
REVOKE ALL ON TABLE public.app_config FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.app_config TO anon;
GRANT ALL ON TABLE public.app_config TO authenticated;
GRANT ALL ON TABLE public.app_config TO service_role;

-- ACL: public.TABLE async_jobs
REVOKE ALL ON TABLE public.async_jobs FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.async_jobs TO anon;
GRANT ALL ON TABLE public.async_jobs TO authenticated;
GRANT ALL ON TABLE public.async_jobs TO service_role;

-- ACL: public.TABLE chat_active_turns
REVOKE ALL ON TABLE public.chat_active_turns FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.chat_active_turns TO service_role;

-- ACL: public.TABLE chat_contexts
REVOKE ALL ON TABLE public.chat_contexts FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.chat_contexts TO service_role;

-- ACL: public.TABLE chat_response_references
REVOKE ALL ON TABLE public.chat_response_references FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.chat_response_references TO service_role;

-- ACL: public.TABLE conversation_checkpoints
REVOKE ALL ON TABLE public.conversation_checkpoints FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.conversation_checkpoints TO service_role;

-- ACL: public.TABLE conversation_focus
REVOKE ALL ON TABLE public.conversation_focus FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.conversation_focus TO service_role;

-- ACL: public.TABLE daily_fortunes
REVOKE ALL ON TABLE public.daily_fortunes FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.daily_fortunes TO service_role;

-- ACL: public.TABLE diaries
REVOKE ALL ON TABLE public.diaries FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.diaries TO anon;
GRANT ALL ON TABLE public.diaries TO authenticated;
GRANT ALL ON TABLE public.diaries TO service_role;

-- ACL: public.TABLE diary_claim_sources
REVOKE ALL ON TABLE public.diary_claim_sources FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.diary_claim_sources TO service_role;

-- ACL: public.TABLE diary_gen_claims
REVOKE ALL ON TABLE public.diary_gen_claims FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.diary_gen_claims TO anon;
GRANT ALL ON TABLE public.diary_gen_claims TO authenticated;
GRANT ALL ON TABLE public.diary_gen_claims TO service_role;

-- ACL: public.TABLE diary_generation_results
REVOKE ALL ON TABLE public.diary_generation_results FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.diary_generation_results TO service_role;

-- ACL: public.TABLE diary_recall_documents
REVOKE ALL ON TABLE public.diary_recall_documents FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.diary_recall_documents TO service_role;

-- ACL: public.TABLE feedback
REVOKE ALL ON TABLE public.feedback FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.feedback TO anon;
GRANT ALL ON TABLE public.feedback TO authenticated;
GRANT ALL ON TABLE public.feedback TO service_role;

-- ACL: public.TABLE fortune_ad_sessions
REVOKE ALL ON TABLE public.fortune_ad_sessions FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.fortune_ad_sessions TO service_role;

-- ACL: public.TABLE fortune_profiles
REVOKE ALL ON TABLE public.fortune_profiles FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.fortune_profiles TO service_role;

-- ACL: public.TABLE greetings
REVOKE ALL ON TABLE public.greetings FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.greetings TO anon;
GRANT ALL ON TABLE public.greetings TO authenticated;
GRANT ALL ON TABLE public.greetings TO service_role;

-- ACL: public.TABLE hay_transactions
REVOKE ALL ON TABLE public.hay_transactions FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.hay_transactions TO anon;
GRANT ALL ON TABLE public.hay_transactions TO authenticated;
GRANT ALL ON TABLE public.hay_transactions TO service_role;

-- ACL: public.SEQUENCE hay_transactions_id_seq
REVOKE ALL ON SEQUENCE public.hay_transactions_id_seq FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON SEQUENCE public.hay_transactions_id_seq TO anon;
GRANT ALL ON SEQUENCE public.hay_transactions_id_seq TO authenticated;
GRANT ALL ON SEQUENCE public.hay_transactions_id_seq TO service_role;

-- ACL: public.TABLE idempotency_keys
REVOKE ALL ON TABLE public.idempotency_keys FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.idempotency_keys TO anon;
GRANT ALL ON TABLE public.idempotency_keys TO authenticated;
GRANT ALL ON TABLE public.idempotency_keys TO service_role;

-- ACL: public.TABLE job_attempts
REVOKE ALL ON TABLE public.job_attempts FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.job_attempts TO anon;
GRANT ALL ON TABLE public.job_attempts TO authenticated;
GRANT ALL ON TABLE public.job_attempts TO service_role;

-- ACL: public.TABLE mem0_ingest_candidate_sources
REVOKE ALL ON TABLE public.mem0_ingest_candidate_sources FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.mem0_ingest_candidate_sources TO anon;
GRANT ALL ON TABLE public.mem0_ingest_candidate_sources TO authenticated;
GRANT ALL ON TABLE public.mem0_ingest_candidate_sources TO service_role;

-- ACL: public.TABLE mem0_ingest_candidates
REVOKE ALL ON TABLE public.mem0_ingest_candidates FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.mem0_ingest_candidates TO anon;
GRANT ALL ON TABLE public.mem0_ingest_candidates TO authenticated;
GRANT ALL ON TABLE public.mem0_ingest_candidates TO service_role;

-- ACL: public.TABLE mem0_memory_registry
REVOKE ALL ON TABLE public.mem0_memory_registry FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.mem0_memory_registry TO anon;
GRANT ALL ON TABLE public.mem0_memory_registry TO authenticated;
GRANT ALL ON TABLE public.mem0_memory_registry TO service_role;

-- ACL: public.TABLE mem0_memory_sources
REVOKE ALL ON TABLE public.mem0_memory_sources FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.mem0_memory_sources TO anon;
GRANT ALL ON TABLE public.mem0_memory_sources TO authenticated;
GRANT ALL ON TABLE public.mem0_memory_sources TO service_role;

-- ACL: public.TABLE memory_pipeline_states
REVOKE ALL ON TABLE public.memory_pipeline_states FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.memory_pipeline_states TO anon;
GRANT ALL ON TABLE public.memory_pipeline_states TO authenticated;
GRANT ALL ON TABLE public.memory_pipeline_states TO service_role;

-- ACL: public.TABLE messages
REVOKE ALL ON TABLE public.messages FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.messages TO anon;
GRANT ALL ON TABLE public.messages TO authenticated;
GRANT ALL ON TABLE public.messages TO service_role;

-- ACL: public.SEQUENCE messages_id_seq
REVOKE ALL ON SEQUENCE public.messages_id_seq FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON SEQUENCE public.messages_id_seq TO anon;
GRANT ALL ON SEQUENCE public.messages_id_seq TO authenticated;
GRANT ALL ON SEQUENCE public.messages_id_seq TO service_role;

-- ACL: public.TABLE moly_life_ments
REVOKE ALL ON TABLE public.moly_life_ments FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.moly_life_ments TO anon;
GRANT ALL ON TABLE public.moly_life_ments TO authenticated;
GRANT ALL ON TABLE public.moly_life_ments TO service_role;

-- ACL: public.TABLE mood_entries
REVOKE ALL ON TABLE public.mood_entries FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.mood_entries TO service_role;

-- ACL: public.TABLE order_items
REVOKE ALL ON TABLE public.order_items FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.order_items TO anon;
GRANT ALL ON TABLE public.order_items TO authenticated;
GRANT ALL ON TABLE public.order_items TO service_role;

-- ACL: public.TABLE orders
REVOKE ALL ON TABLE public.orders FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.orders TO anon;
GRANT ALL ON TABLE public.orders TO authenticated;
GRANT ALL ON TABLE public.orders TO service_role;

-- ACL: public.TABLE payments
REVOKE ALL ON TABLE public.payments FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.payments TO anon;
GRANT ALL ON TABLE public.payments TO authenticated;
GRANT ALL ON TABLE public.payments TO service_role;

-- ACL: public.TABLE privacy_ledger_events
REVOKE ALL ON TABLE public.privacy_ledger_events FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.privacy_ledger_events TO service_role;

-- ACL: public.SEQUENCE privacy_ledger_events_id_seq
REVOKE ALL ON SEQUENCE public.privacy_ledger_events_id_seq FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON SEQUENCE public.privacy_ledger_events_id_seq TO anon;
GRANT ALL ON SEQUENCE public.privacy_ledger_events_id_seq TO authenticated;
GRANT ALL ON SEQUENCE public.privacy_ledger_events_id_seq TO service_role;

-- ACL: public.TABLE privacy_subject_barriers
REVOKE ALL ON TABLE public.privacy_subject_barriers FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.privacy_subject_barriers TO service_role;

-- ACL: public.TABLE products
REVOKE ALL ON TABLE public.products FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.products TO anon;
GRANT ALL ON TABLE public.products TO authenticated;
GRANT ALL ON TABLE public.products TO service_role;

-- ACL: public.TABLE profiles
REVOKE ALL ON TABLE public.profiles FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.profiles TO anon;
GRANT ALL ON TABLE public.profiles TO authenticated;
GRANT ALL ON TABLE public.profiles TO service_role;

-- ACL: public.TABLE provider_backoffs
REVOKE ALL ON TABLE public.provider_backoffs FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.provider_backoffs TO anon;
GRANT ALL ON TABLE public.provider_backoffs TO authenticated;
GRANT ALL ON TABLE public.provider_backoffs TO service_role;

-- ACL: public.TABLE relationship_events
REVOKE ALL ON TABLE public.relationship_events FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.relationship_events TO anon;
GRANT ALL ON TABLE public.relationship_events TO authenticated;
GRANT ALL ON TABLE public.relationship_events TO service_role;

-- ACL: public.SEQUENCE relationship_events_id_seq
REVOKE ALL ON SEQUENCE public.relationship_events_id_seq FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON SEQUENCE public.relationship_events_id_seq TO anon;
GRANT ALL ON SEQUENCE public.relationship_events_id_seq TO authenticated;
GRANT ALL ON SEQUENCE public.relationship_events_id_seq TO service_role;

-- ACL: public.TABLE relationship_profile_renders
REVOKE ALL ON TABLE public.relationship_profile_renders FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.relationship_profile_renders TO anon;
GRANT ALL ON TABLE public.relationship_profile_renders TO authenticated;
GRANT ALL ON TABLE public.relationship_profile_renders TO service_role;

-- ACL: public.TABLE revenuecat_events
REVOKE ALL ON TABLE public.revenuecat_events FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.revenuecat_events TO anon;
GRANT ALL ON TABLE public.revenuecat_events TO authenticated;
GRANT ALL ON TABLE public.revenuecat_events TO service_role;

-- ACL: public.TABLE reward_ad_sessions
REVOKE ALL ON TABLE public.reward_ad_sessions FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.reward_ad_sessions TO anon;
GRANT ALL ON TABLE public.reward_ad_sessions TO authenticated;
GRANT ALL ON TABLE public.reward_ad_sessions TO service_role;

-- ACL: public.TABLE routine_completions
REVOKE ALL ON TABLE public.routine_completions FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.routine_completions TO anon;
GRANT ALL ON TABLE public.routine_completions TO authenticated;
GRANT ALL ON TABLE public.routine_completions TO service_role;

-- ACL: public.SEQUENCE routine_completions_id_seq
REVOKE ALL ON SEQUENCE public.routine_completions_id_seq FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON SEQUENCE public.routine_completions_id_seq TO anon;
GRANT ALL ON SEQUENCE public.routine_completions_id_seq TO authenticated;
GRANT ALL ON SEQUENCE public.routine_completions_id_seq TO service_role;

-- ACL: public.TABLE routines
REVOKE ALL ON TABLE public.routines FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.routines TO anon;
GRANT ALL ON TABLE public.routines TO authenticated;
GRANT ALL ON TABLE public.routines TO service_role;

-- ACL: public.TABLE schema_migrations
REVOKE ALL ON TABLE public.schema_migrations FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.schema_migrations TO service_role;

-- ACL: public.TABLE shadow_prompt_traces
REVOKE ALL ON TABLE public.shadow_prompt_traces FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.shadow_prompt_traces TO service_role;

-- ACL: public.TABLE subscription_hay_grants
REVOKE ALL ON TABLE public.subscription_hay_grants FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.subscription_hay_grants TO anon;
GRANT ALL ON TABLE public.subscription_hay_grants TO authenticated;
GRANT ALL ON TABLE public.subscription_hay_grants TO service_role;

-- ACL: public.TABLE subscriptions
REVOKE ALL ON TABLE public.subscriptions FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.subscriptions TO anon;
GRANT ALL ON TABLE public.subscriptions TO authenticated;
GRANT ALL ON TABLE public.subscriptions TO service_role;

-- ACL: public.TABLE user_daily_stats
REVOKE ALL ON TABLE public.user_daily_stats FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.user_daily_stats TO anon;
GRANT ALL ON TABLE public.user_daily_stats TO authenticated;
GRANT ALL ON TABLE public.user_daily_stats TO service_role;

-- ACL: public.SEQUENCE user_daily_stats_id_seq
REVOKE ALL ON SEQUENCE public.user_daily_stats_id_seq FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON SEQUENCE public.user_daily_stats_id_seq TO anon;
GRANT ALL ON SEQUENCE public.user_daily_stats_id_seq TO authenticated;
GRANT ALL ON SEQUENCE public.user_daily_stats_id_seq TO service_role;

-- ACL: public.TABLE user_devices
REVOKE ALL ON TABLE public.user_devices FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.user_devices TO anon;
GRANT ALL ON TABLE public.user_devices TO authenticated;
GRANT ALL ON TABLE public.user_devices TO service_role;

-- ACL: public.TABLE user_interaction_contract_items
REVOKE ALL ON TABLE public.user_interaction_contract_items FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.user_interaction_contract_items TO anon;
GRANT ALL ON TABLE public.user_interaction_contract_items TO authenticated;
GRANT ALL ON TABLE public.user_interaction_contract_items TO service_role;

-- ACL: public.TABLE user_interaction_contracts
REVOKE ALL ON TABLE public.user_interaction_contracts FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.user_interaction_contracts TO anon;
GRANT ALL ON TABLE public.user_interaction_contracts TO authenticated;
GRANT ALL ON TABLE public.user_interaction_contracts TO service_role;

-- ACL: public.TABLE user_items
REVOKE ALL ON TABLE public.user_items FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.user_items TO anon;
GRANT ALL ON TABLE public.user_items TO authenticated;
GRANT ALL ON TABLE public.user_items TO service_role;

-- ACL: public.TABLE user_notification_settings
REVOKE ALL ON TABLE public.user_notification_settings FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.user_notification_settings TO anon;
GRANT ALL ON TABLE public.user_notification_settings TO authenticated;
GRANT ALL ON TABLE public.user_notification_settings TO service_role;

-- ACL: public.TABLE user_relationship_states
REVOKE ALL ON TABLE public.user_relationship_states FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.user_relationship_states TO anon;
GRANT ALL ON TABLE public.user_relationship_states TO authenticated;
GRANT ALL ON TABLE public.user_relationship_states TO service_role;

-- ACL: public.TABLE user_schedules
REVOKE ALL ON TABLE public.user_schedules FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.user_schedules TO service_role;

-- Auth signup hook (owned by this application).
CREATE TRIGGER on_auth_user_created AFTER INSERT ON auth.users FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- Topic offers and prepared conversation openings.
CREATE TABLE IF NOT EXISTS public.user_topic_states (
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  placement text NOT NULL CHECK (placement = 'home_blind'),
  offer_id uuid NOT NULL UNIQUE,
  offer_sequence bigint NOT NULL CHECK (offer_sequence > 0),
  topic_id text NOT NULL,
  topic_revision text NOT NULL,
  questions jsonb NOT NULL,
  day_high_watermark date NOT NULL,
  completed boolean NOT NULL DEFAULT false,
  daily_open_count smallint NOT NULL DEFAULT 0 CONSTRAINT topic_daily_open_limit CHECK (daily_open_count BETWEEN 0 AND 2),
  offer_opened boolean NOT NULL DEFAULT false,
  offered_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (user_id, placement)
);

CREATE TABLE IF NOT EXISTS public.chat_topic_entries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  placement text NOT NULL,
  offer_id uuid NOT NULL,
  offer_sequence bigint NOT NULL,
  topic_id text NOT NULL,
  topic_revision text NOT NULL,
  questions jsonb NOT NULL,
  context_revision bigint NOT NULL,
  state text NOT NULL DEFAULT 'pending',
  timezone_name text NOT NULL,
  local_date date NOT NULL,
  created_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  committed_message_id bigint,
  first_user_message_id bigint,
  FOREIGN KEY (user_id, committed_message_id) REFERENCES public.messages(user_id, id) ON DELETE CASCADE,
  FOREIGN KEY (user_id, first_user_message_id) REFERENCES public.messages(user_id, id) ON DELETE CASCADE,
  CONSTRAINT topic_entry_state CHECK (state IN ('pending','committed','superseded')),
  CONSTRAINT topic_entry_expiration CHECK (expires_at > created_at),
  CONSTRAINT topic_entry_committed_links CHECK (
    state <> 'committed' OR (committed_message_id IS NOT NULL AND first_user_message_id IS NOT NULL)
  ),
  CONSTRAINT topic_entry_pending_links CHECK (
    state <> 'pending' OR (committed_message_id IS NULL AND first_user_message_id IS NULL)
  )
);
CREATE UNIQUE INDEX IF NOT EXISTS topic_entry_one_pending
  ON public.chat_topic_entries(user_id) WHERE state = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS topic_entry_one_answer
  ON public.chat_topic_entries(user_id, offer_id) WHERE first_user_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS topic_entry_offer_lookup
  ON public.chat_topic_entries(user_id, offer_id, created_at);
CREATE INDEX IF NOT EXISTS topic_entry_expiry
  ON public.chat_topic_entries(expires_at) WHERE state <> 'committed';

ALTER TABLE public.user_topic_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.chat_topic_entries ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.user_topic_states, public.chat_topic_entries FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON public.user_topic_states, public.chat_topic_entries TO service_role;

COMMIT;
