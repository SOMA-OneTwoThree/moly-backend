--
-- PostgreSQL database dump
--

\restrict TnNnIYPqyStlVWWLurwYkyPhiMTZB0FnsVqS0mxBOgLR7HcfO9kJbltQEykFkAo

-- Dumped from database version 17.6
-- Dumped by pg_dump version 17.10 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: vecs; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA vecs;


--
-- Name: pg_stat_statements; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA extensions;


--
-- Name: EXTENSION pg_stat_statements; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_stat_statements IS 'track planning and execution statistics of all SQL statements executed';


--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: pgcrypto; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;


--
-- Name: EXTENSION pgcrypto; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pgcrypto IS 'cryptographic functions';


--
-- Name: supabase_vault; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS supabase_vault WITH SCHEMA vault;


--
-- Name: EXTENSION supabase_vault; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION supabase_vault IS 'Supabase Vault Extension';


--
-- Name: uuid-ossp; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA extensions;


--
-- Name: EXTENSION "uuid-ossp"; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION "uuid-ossp" IS 'generate universally unique identifiers (UUIDs)';


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


--
-- Name: bootstrap_user(uuid, timestamp with time zone); Type: FUNCTION; Schema: public; Owner: -
--

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


--
-- Name: create_privacy_barrier_for_profile(); Type: FUNCTION; Schema: public; Owner: -
--

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


--
-- Name: delete_user_memories(uuid); Type: FUNCTION; Schema: public; Owner: -
--

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


--
-- Name: guard_normalized_memory_snapshot(); Type: FUNCTION; Schema: public; Owner: -
--

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


--
-- Name: handle_new_user(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.handle_new_user() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
BEGIN
  PERFORM public.bootstrap_user(NEW.id, NEW.created_at);
  RETURN NEW;
END;
$$;


--
-- Name: normalize_content_language(text); Type: FUNCTION; Schema: public; Owner: -
--

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


--
-- Name: normalize_profile_language(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.normalize_profile_language() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
BEGIN
  NEW.language := public.normalize_content_language(NEW.language);
  RETURN NEW;
END;
$$;


--
-- Name: set_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: ai_price_catalog; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: ai_usage_ledger; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: app_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.app_config (
    key text NOT NULL,
    value jsonb NOT NULL,
    description text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: async_jobs; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: chat_active_turns; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: chat_contexts; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: chat_response_references; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: conversation_checkpoints; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: conversation_focus; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: diaries; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: diary_claim_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.diary_claim_sources (
    user_id uuid NOT NULL,
    diary_id uuid NOT NULL,
    message_id bigint NOT NULL,
    source_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: diary_gen_claims; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.diary_gen_claims (
    user_id uuid NOT NULL,
    target_date date NOT NULL,
    claimed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: diary_generation_results; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.diary_generation_results (
    user_id uuid NOT NULL,
    target_date date NOT NULL,
    status text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT diary_generation_results_status_check CHECK ((status = 'no_entry'::text))
);


--
-- Name: diary_recall_documents; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: feedback; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.feedback (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    message text NOT NULL,
    contact text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT feedback_contact_check CHECK ((char_length(contact) <= 200)),
    CONSTRAINT feedback_message_check CHECK ((char_length(message) <= 2000))
);


--
-- Name: greetings; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: hay_transactions; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: hay_transactions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.hay_transactions ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.hay_transactions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: idempotency_keys; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: job_attempts; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: mem0_ingest_candidate_sources; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: mem0_ingest_candidates; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: COLUMN mem0_ingest_candidates.category; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.mem0_ingest_candidates.category IS '기억의 종류. 허용 목록은 코드가 갖는다(mem0_extractor.CATEGORIES). NULL = v3 이전에 뽑힌 기억.';


--
-- Name: mem0_memory_registry; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: COLUMN mem0_memory_registry.category; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.mem0_memory_registry.category IS '기억의 종류. 회상에서 오래 남는 종류를 앞세우는 데 쓴다. NULL = v3 이전에 뽑힌 기억.';


--
-- Name: COLUMN mem0_memory_registry.last_reconsolidated_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.mem0_memory_registry.last_reconsolidated_at IS '마지막으로 재판정 비교에 참여한 시각. NULL = 아직 한 번도 안 봤다.';


--
-- Name: mem0_memory_sources; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: memory_pipeline_states; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: messages; Type: TABLE; Schema: public; Owner: -
--

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
    CONSTRAINT messages_kind_check CHECK ((kind = ANY (ARRAY['normal'::text, 'greeting'::text]))),
    CONSTRAINT messages_sender_check CHECK ((sender = ANY (ARRAY['user'::text, 'moly'::text]))),
    CONSTRAINT messages_turn_position_ck CHECK ((((turn_seq IS NULL) AND (turn_position IS NULL)) OR ((turn_seq > 0) AND ((turn_position >= 0) AND (turn_position <= 2)))))
);


--
-- Name: messages_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.messages ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.messages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: moly_life_ments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.moly_life_ments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    content text NOT NULL,
    weather text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    diary_date date,
    CONSTRAINT moly_life_ments_weather_check CHECK ((weather = ANY (ARRAY['sunny'::text, 'cloudy'::text, 'rainy'::text, 'windy'::text])))
);


--
-- Name: order_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.order_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    order_id uuid NOT NULL,
    product_id uuid NOT NULL,
    quantity integer DEFAULT 1 NOT NULL,
    unit_price integer NOT NULL,
    CONSTRAINT order_items_quantity_check CHECK ((quantity > 0)),
    CONSTRAINT order_items_unit_price_check CHECK ((unit_price >= 0))
);


--
-- Name: orders; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: payments; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: privacy_ledger_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.privacy_ledger_events (
    id bigint NOT NULL,
    operation_id uuid NOT NULL,
    user_id uuid NOT NULL,
    event text NOT NULL,
    high_watermark bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: privacy_ledger_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.privacy_ledger_events ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.privacy_ledger_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: privacy_subject_barriers; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: products; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: profiles; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: provider_backoffs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.provider_backoffs (
    provider text NOT NULL,
    model text NOT NULL,
    lane text NOT NULL,
    blocked_until timestamp with time zone NOT NULL,
    reason text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: relationship_events; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: relationship_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.relationship_events ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.relationship_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: relationship_profile_renders; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: revenuecat_events; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: reward_ad_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reward_ad_sessions (
    session_id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    activity_date date NOT NULL,
    ssv_transaction_id text,
    granted boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: routine_completions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.routine_completions (
    id bigint NOT NULL,
    routine_id uuid NOT NULL,
    user_id uuid NOT NULL,
    activity_date date NOT NULL,
    completed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: routine_completions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.routine_completions ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.routine_completions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: routines; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: schema_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_migrations (
    migration_name text NOT NULL,
    checksum_sha256 text NOT NULL,
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    applied_by text DEFAULT CURRENT_USER NOT NULL
);


--
-- Name: shadow_prompt_traces; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: subscription_hay_grants; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: subscriptions; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: user_daily_stats; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: user_daily_stats_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.user_daily_stats ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.user_daily_stats_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: user_devices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_devices (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    platform text NOT NULL,
    push_token text NOT NULL,
    last_active_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_devices_platform_check CHECK ((platform = ANY (ARRAY['ios'::text, 'android'::text])))
);


--
-- Name: user_interaction_contract_items; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: user_interaction_contracts; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: user_items; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: user_notification_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_notification_settings (
    user_id uuid NOT NULL,
    type text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    CONSTRAINT user_notification_settings_type_check CHECK ((type = ANY (ARRAY['morning_diary'::text, 'evening_chat'::text])))
);


--
-- Name: user_relationship_states; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: user_schedules; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: moly_memories_v2; Type: TABLE; Schema: vecs; Owner: -
--

CREATE TABLE vecs.moly_memories_v2 (
    id character varying NOT NULL,
    vec public.vector(1536) NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
)
WITH (autovacuum_vacuum_scale_factor='0.02', toast.autovacuum_vacuum_scale_factor='0.02');


--
-- Name: ai_price_catalog ai_price_catalog_catalog_version_provider_model_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_price_catalog
    ADD CONSTRAINT ai_price_catalog_catalog_version_provider_model_key UNIQUE (catalog_version, provider, model);


--
-- Name: ai_price_catalog ai_price_catalog_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_price_catalog
    ADD CONSTRAINT ai_price_catalog_pkey PRIMARY KEY (id);


--
-- Name: ai_usage_ledger ai_usage_ledger_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_usage_ledger
    ADD CONSTRAINT ai_usage_ledger_pkey PRIMARY KEY (call_id);


--
-- Name: app_config app_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_config
    ADD CONSTRAINT app_config_pkey PRIMARY KEY (key);


--
-- Name: async_jobs async_jobs_job_type_dedup_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.async_jobs
    ADD CONSTRAINT async_jobs_job_type_dedup_key_key UNIQUE (job_type, dedup_key);


--
-- Name: async_jobs async_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.async_jobs
    ADD CONSTRAINT async_jobs_pkey PRIMARY KEY (id);


--
-- Name: chat_active_turns chat_active_turns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_active_turns
    ADD CONSTRAINT chat_active_turns_pkey PRIMARY KEY (user_id);


--
-- Name: chat_contexts chat_contexts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_contexts
    ADD CONSTRAINT chat_contexts_pkey PRIMARY KEY (user_id);


--
-- Name: chat_response_references chat_response_references_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_response_references
    ADD CONSTRAINT chat_response_references_pkey PRIMARY KEY (id);


--
-- Name: chat_response_references chat_response_references_user_id_reply_message_id_ordinal_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_response_references
    ADD CONSTRAINT chat_response_references_user_id_reply_message_id_ordinal_key UNIQUE (user_id, reply_message_id, ordinal);


--
-- Name: conversation_checkpoints conversation_checkpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_checkpoints
    ADD CONSTRAINT conversation_checkpoints_pkey PRIMARY KEY (id);


--
-- Name: conversation_checkpoints conversation_checkpoints_user_id_through_message_id_source__key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_checkpoints
    ADD CONSTRAINT conversation_checkpoints_user_id_through_message_id_source__key UNIQUE (user_id, through_message_id, source_hash);


--
-- Name: conversation_focus conversation_focus_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_focus
    ADD CONSTRAINT conversation_focus_pkey PRIMARY KEY (user_id);


--
-- Name: diaries diaries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diaries
    ADD CONSTRAINT diaries_pkey PRIMARY KEY (id);


--
-- Name: diary_claim_sources diary_claim_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diary_claim_sources
    ADD CONSTRAINT diary_claim_sources_pkey PRIMARY KEY (user_id, diary_id, message_id);


--
-- Name: diary_gen_claims diary_gen_claims_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diary_gen_claims
    ADD CONSTRAINT diary_gen_claims_pkey PRIMARY KEY (user_id, target_date);


--
-- Name: diary_generation_results diary_generation_results_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diary_generation_results
    ADD CONSTRAINT diary_generation_results_pkey PRIMARY KEY (user_id, target_date);


--
-- Name: diary_recall_documents diary_recall_documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diary_recall_documents
    ADD CONSTRAINT diary_recall_documents_pkey PRIMARY KEY (user_id, diary_id);


--
-- Name: feedback feedback_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feedback
    ADD CONSTRAINT feedback_pkey PRIMARY KEY (id);


--
-- Name: greetings greetings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.greetings
    ADD CONSTRAINT greetings_pkey PRIMARY KEY (id);


--
-- Name: greetings greetings_user_ctx_date_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.greetings
    ADD CONSTRAINT greetings_user_ctx_date_uq UNIQUE (user_id, context, activity_date);


--
-- Name: hay_transactions hay_transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hay_transactions
    ADD CONSTRAINT hay_transactions_pkey PRIMARY KEY (id);


--
-- Name: idempotency_keys idempotency_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.idempotency_keys
    ADD CONSTRAINT idempotency_keys_pkey PRIMARY KEY (user_id, key);


--
-- Name: job_attempts job_attempts_job_id_attempt_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_attempts
    ADD CONSTRAINT job_attempts_job_id_attempt_key UNIQUE (job_id, attempt);


--
-- Name: job_attempts job_attempts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_attempts
    ADD CONSTRAINT job_attempts_pkey PRIMARY KEY (id);


--
-- Name: mem0_ingest_candidate_sources mem0_ingest_candidate_sources_candidate_id_source_message_i_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mem0_ingest_candidate_sources
    ADD CONSTRAINT mem0_ingest_candidate_sources_candidate_id_source_message_i_key UNIQUE (candidate_id, source_message_id, evidence_start_utf8, evidence_end_utf8);


--
-- Name: mem0_ingest_candidates mem0_ingest_candidates_id_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mem0_ingest_candidates
    ADD CONSTRAINT mem0_ingest_candidates_id_user_id_key UNIQUE (id, user_id);


--
-- Name: mem0_ingest_candidates mem0_ingest_candidates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mem0_ingest_candidates
    ADD CONSTRAINT mem0_ingest_candidates_pkey PRIMARY KEY (id);


--
-- Name: mem0_ingest_candidates mem0_ingest_candidates_user_id_turn_seq_candidate_hash_sche_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mem0_ingest_candidates
    ADD CONSTRAINT mem0_ingest_candidates_user_id_turn_seq_candidate_hash_sche_key UNIQUE (user_id, turn_seq, candidate_hash, schema_version, repair_generation);


--
-- Name: mem0_memory_registry mem0_memory_registry_id_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mem0_memory_registry
    ADD CONSTRAINT mem0_memory_registry_id_user_id_key UNIQUE (id, user_id);


--
-- Name: mem0_memory_registry mem0_memory_registry_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mem0_memory_registry
    ADD CONSTRAINT mem0_memory_registry_pkey PRIMARY KEY (id);


--
-- Name: mem0_memory_registry mem0_memory_registry_user_id_provider_collection_version_pr_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mem0_memory_registry
    ADD CONSTRAINT mem0_memory_registry_user_id_provider_collection_version_pr_key UNIQUE (user_id, provider, collection_version, provider_memory_id);


--
-- Name: mem0_memory_sources mem0_memory_sources_registry_id_source_message_id_evidence__key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mem0_memory_sources
    ADD CONSTRAINT mem0_memory_sources_registry_id_source_message_id_evidence__key UNIQUE (registry_id, source_message_id, evidence_start_utf8, evidence_end_utf8);


--
-- Name: memory_pipeline_states memory_pipeline_states_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_pipeline_states
    ADD CONSTRAINT memory_pipeline_states_pkey PRIMARY KEY (user_id);


--
-- Name: messages messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (id);


--
-- Name: moly_life_ments moly_life_ments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.moly_life_ments
    ADD CONSTRAINT moly_life_ments_pkey PRIMARY KEY (id);


--
-- Name: order_items order_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_pkey PRIMARY KEY (id);


--
-- Name: orders orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_pkey PRIMARY KEY (id);


--
-- Name: payments payments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_pkey PRIMARY KEY (id);


--
-- Name: payments payments_store_transaction_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_store_transaction_id_key UNIQUE (store_transaction_id);


--
-- Name: privacy_ledger_events privacy_ledger_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.privacy_ledger_events
    ADD CONSTRAINT privacy_ledger_events_pkey PRIMARY KEY (id);


--
-- Name: privacy_subject_barriers privacy_subject_barriers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.privacy_subject_barriers
    ADD CONSTRAINT privacy_subject_barriers_pkey PRIMARY KEY (user_id);


--
-- Name: products products_app_store_product_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_app_store_product_id_key UNIQUE (app_store_product_id);


--
-- Name: products products_id_slot_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_id_slot_uq UNIQUE (id, slot);


--
-- Name: products products_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_pkey PRIMARY KEY (id);


--
-- Name: products products_play_store_product_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_play_store_product_id_key UNIQUE (play_store_product_id);


--
-- Name: profiles profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.profiles
    ADD CONSTRAINT profiles_pkey PRIMARY KEY (id);


--
-- Name: provider_backoffs provider_backoffs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provider_backoffs
    ADD CONSTRAINT provider_backoffs_pkey PRIMARY KEY (provider, model, lane);


--
-- Name: relationship_events relationship_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relationship_events
    ADD CONSTRAINT relationship_events_pkey PRIMARY KEY (id);


--
-- Name: relationship_events relationship_events_user_id_dedup_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relationship_events
    ADD CONSTRAINT relationship_events_user_id_dedup_key_key UNIQUE (user_id, dedup_key);


--
-- Name: relationship_profile_renders relationship_profile_renders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relationship_profile_renders
    ADD CONSTRAINT relationship_profile_renders_pkey PRIMARY KEY (id);


--
-- Name: relationship_profile_renders relationship_profile_renders_user_id_prompt_revision_profil_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relationship_profile_renders
    ADD CONSTRAINT relationship_profile_renders_user_id_prompt_revision_profil_key UNIQUE (user_id, prompt_revision, profile_relationship_revision, locale, renderer_version);


--
-- Name: revenuecat_events revenuecat_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.revenuecat_events
    ADD CONSTRAINT revenuecat_events_pkey PRIMARY KEY (event_id);


--
-- Name: reward_ad_sessions reward_ad_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reward_ad_sessions
    ADD CONSTRAINT reward_ad_sessions_pkey PRIMARY KEY (session_id);


--
-- Name: reward_ad_sessions reward_ad_sessions_ssv_transaction_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reward_ad_sessions
    ADD CONSTRAINT reward_ad_sessions_ssv_transaction_id_key UNIQUE (ssv_transaction_id);


--
-- Name: routine_completions routine_completions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.routine_completions
    ADD CONSTRAINT routine_completions_pkey PRIMARY KEY (id);


--
-- Name: routine_completions routine_completions_routine_date_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.routine_completions
    ADD CONSTRAINT routine_completions_routine_date_uq UNIQUE (routine_id, activity_date);


--
-- Name: routines routines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.routines
    ADD CONSTRAINT routines_pkey PRIMARY KEY (id);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (migration_name);


--
-- Name: shadow_prompt_traces shadow_prompt_traces_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shadow_prompt_traces
    ADD CONSTRAINT shadow_prompt_traces_pkey PRIMARY KEY (id);


--
-- Name: shadow_prompt_traces shadow_prompt_traces_turn_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shadow_prompt_traces
    ADD CONSTRAINT shadow_prompt_traces_turn_uniq UNIQUE (user_id, turn_seq, assembler_version);


--
-- Name: subscription_hay_grants subscription_hay_grants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_hay_grants
    ADD CONSTRAINT subscription_hay_grants_pkey PRIMARY KEY (id);


--
-- Name: subscription_hay_grants subscription_hay_grants_user_plan_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_hay_grants
    ADD CONSTRAINT subscription_hay_grants_user_plan_uq UNIQUE (user_id, plan);


--
-- Name: subscriptions subscriptions_original_transaction_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_original_transaction_id_key UNIQUE (original_transaction_id);


--
-- Name: subscriptions subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_pkey PRIMARY KEY (id);


--
-- Name: user_daily_stats user_daily_stats_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_daily_stats
    ADD CONSTRAINT user_daily_stats_pkey PRIMARY KEY (id);


--
-- Name: user_daily_stats user_daily_stats_user_date_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_daily_stats
    ADD CONSTRAINT user_daily_stats_user_date_uq UNIQUE (user_id, activity_date);


--
-- Name: user_devices user_devices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_devices
    ADD CONSTRAINT user_devices_pkey PRIMARY KEY (id);


--
-- Name: user_devices user_devices_push_token_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_devices
    ADD CONSTRAINT user_devices_push_token_key UNIQUE (push_token);


--
-- Name: user_interaction_contract_items user_interaction_contract_items_contract_id_item_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_interaction_contract_items
    ADD CONSTRAINT user_interaction_contract_items_contract_id_item_key_key UNIQUE (contract_id, item_key);


--
-- Name: user_interaction_contract_items user_interaction_contract_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_interaction_contract_items
    ADD CONSTRAINT user_interaction_contract_items_pkey PRIMARY KEY (id);


--
-- Name: user_interaction_contracts user_interaction_contracts_id_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_interaction_contracts
    ADD CONSTRAINT user_interaction_contracts_id_user_id_key UNIQUE (id, user_id);


--
-- Name: user_interaction_contracts user_interaction_contracts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_interaction_contracts
    ADD CONSTRAINT user_interaction_contracts_pkey PRIMARY KEY (id);


--
-- Name: user_interaction_contracts user_interaction_contracts_user_id_locale_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_interaction_contracts
    ADD CONSTRAINT user_interaction_contracts_user_id_locale_version_key UNIQUE (user_id, locale, version);


--
-- Name: user_items user_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_items
    ADD CONSTRAINT user_items_pkey PRIMARY KEY (id);


--
-- Name: user_items user_items_user_product_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_items
    ADD CONSTRAINT user_items_user_product_uq UNIQUE (user_id, product_id);


--
-- Name: user_notification_settings user_notification_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_notification_settings
    ADD CONSTRAINT user_notification_settings_pkey PRIMARY KEY (user_id, type);


--
-- Name: user_relationship_states user_relationship_states_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_relationship_states
    ADD CONSTRAINT user_relationship_states_pkey PRIMARY KEY (user_id);


--
-- Name: user_schedules user_schedules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_schedules
    ADD CONSTRAINT user_schedules_pkey PRIMARY KEY (id);


--
-- Name: user_schedules user_schedules_user_kind_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_schedules
    ADD CONSTRAINT user_schedules_user_kind_uniq UNIQUE (user_id, kind);


--
-- Name: moly_memories_v2 moly_memories_v2_pkey; Type: CONSTRAINT; Schema: vecs; Owner: -
--

ALTER TABLE ONLY vecs.moly_memories_v2
    ADD CONSTRAINT moly_memories_v2_pkey PRIMARY KEY (id);


--
-- Name: ai_price_catalog_lookup_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ai_price_catalog_lookup_idx ON public.ai_price_catalog USING btree (provider, model, effective_from DESC);


--
-- Name: ai_usage_ledger_open_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ai_usage_ledger_open_idx ON public.ai_usage_ledger USING btree (status, started_at) WHERE (status = ANY (ARRAY['started'::text, 'unknown_usage'::text]));


--
-- Name: ai_usage_ledger_purpose_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ai_usage_ledger_purpose_idx ON public.ai_usage_ledger USING btree (purpose, started_at DESC);


--
-- Name: ai_usage_ledger_request_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ai_usage_ledger_request_idx ON public.ai_usage_ledger USING btree (provider, provider_request_id) WHERE (provider_request_id IS NOT NULL);


--
-- Name: ai_usage_ledger_user_day_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ai_usage_ledger_user_day_idx ON public.ai_usage_ledger USING btree (user_id, activity_date, lane);


--
-- Name: async_jobs_claim_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX async_jobs_claim_idx ON public.async_jobs USING btree (queue, priority, available_at, created_at) WHERE (state = 'ready'::text);


--
-- Name: async_jobs_reclaim_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX async_jobs_reclaim_idx ON public.async_jobs USING btree (queue, lease_until) WHERE (state = 'running'::text);


--
-- Name: async_jobs_replay_of_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX async_jobs_replay_of_idx ON public.async_jobs USING btree (replay_of) WHERE (replay_of IS NOT NULL);


--
-- Name: async_jobs_replay_operation_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX async_jobs_replay_operation_uq ON public.async_jobs USING btree (replay_of, replay_operation_id) WHERE ((replay_of IS NOT NULL) AND (replay_operation_id IS NOT NULL));


--
-- Name: async_jobs_scrub_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX async_jobs_scrub_idx ON public.async_jobs USING btree (payload_expires_at) WHERE ((payload_redacted_at IS NULL) AND (payload_expires_at IS NOT NULL));


--
-- Name: async_jobs_state_queue_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX async_jobs_state_queue_idx ON public.async_jobs USING btree (state, queue);


--
-- Name: async_jobs_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX async_jobs_user_idx ON public.async_jobs USING btree (user_id) WHERE (user_id IS NOT NULL);


--
-- Name: chat_active_turns_key_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX chat_active_turns_key_uq ON public.chat_active_turns USING btree (user_id, idempotency_key);


--
-- Name: conversation_checkpoints_daily_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX conversation_checkpoints_daily_uq ON public.conversation_checkpoints USING btree (user_id, activity_date_from) WHERE (kind = 'daily_digest'::text);


--
-- Name: conversation_checkpoints_latest_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX conversation_checkpoints_latest_idx ON public.conversation_checkpoints USING btree (user_id, through_message_id DESC);


--
-- Name: conversation_checkpoints_live_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX conversation_checkpoints_live_idx ON public.conversation_checkpoints USING btree (user_id, memory_generation, through_message_id DESC);


--
-- Name: conversation_checkpoints_published_window_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX conversation_checkpoints_published_window_uq ON public.conversation_checkpoints USING btree (user_id, coverage_through_message_id) WHERE ((kind = 'window'::text) AND (publish_state = 'published'::text));


--
-- Name: diaries_one_daily_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX diaries_one_daily_uq ON public.diaries USING btree (user_id, activity_date) WHERE ((kind = ANY (ARRAY['shared_day'::text, 'capi_day'::text])) AND (deleted_at IS NULL));


--
-- Name: diaries_one_welcome_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX diaries_one_welcome_uq ON public.diaries USING btree (user_id) WHERE ((kind = 'welcome'::text) AND (deleted_at IS NULL));


--
-- Name: diaries_user_display_cursor_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX diaries_user_display_cursor_idx ON public.diaries USING btree (user_id, display_date DESC, id DESC) WHERE ((record_status = 'published'::text) AND (deleted_at IS NULL));


--
-- Name: diaries_user_id_id_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX diaries_user_id_id_uq ON public.diaries USING btree (user_id, id);


--
-- Name: diaries_user_published_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX diaries_user_published_idx ON public.diaries USING btree (user_id, published_at);


--
-- Name: diary_claim_sources_user_msg_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX diary_claim_sources_user_msg_idx ON public.diary_claim_sources USING btree (user_id, message_id);


--
-- Name: diary_recall_missing_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX diary_recall_missing_embedding_idx ON public.diary_recall_documents USING btree (updated_at) WHERE (embedding IS NULL);


--
-- Name: feedback_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX feedback_user_idx ON public.feedback USING btree (user_id);


--
-- Name: greetings_committed_message_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX greetings_committed_message_idx ON public.greetings USING btree (committed_message_id) WHERE (committed_message_id IS NOT NULL);


--
-- Name: hay_transactions_order_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX hay_transactions_order_idx ON public.hay_transactions USING btree (order_id);


--
-- Name: hay_transactions_user_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX hay_transactions_user_created_idx ON public.hay_transactions USING btree (user_id, created_at DESC);


--
-- Name: hay_transactions_user_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX hay_transactions_user_id_idx ON public.hay_transactions USING btree (user_id, id);


--
-- Name: idempotency_keys_scrub_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idempotency_keys_scrub_idx ON public.idempotency_keys USING btree (response_expires_at) WHERE (response IS NOT NULL);


--
-- Name: idempotency_reply_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idempotency_reply_idx ON public.idempotency_keys USING btree (user_id, reply_message_id) WHERE (reply_message_id IS NOT NULL);


--
-- Name: job_attempts_open_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX job_attempts_open_idx ON public.job_attempts USING btree (job_id) WHERE (outcome IS NULL);


--
-- Name: job_attempts_queue_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX job_attempts_queue_idx ON public.job_attempts USING btree (queue, started_at DESC);


--
-- Name: mem0_ingest_candidate_sources_user_msg_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mem0_ingest_candidate_sources_user_msg_idx ON public.mem0_ingest_candidate_sources USING btree (user_id, source_message_id);


--
-- Name: mem0_ingest_candidates_open_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mem0_ingest_candidates_open_idx ON public.mem0_ingest_candidates USING btree (user_id, turn_seq) WHERE (status = 'planned'::text);


--
-- Name: mem0_memory_registry_category_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mem0_memory_registry_category_idx ON public.mem0_memory_registry USING btree (user_id, category) WHERE (semantic_status = ANY (ARRAY['active'::text, 'ambiguous'::text]));


--
-- Name: mem0_memory_registry_conflict_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mem0_memory_registry_conflict_idx ON public.mem0_memory_registry USING btree (conflict_group_id) WHERE (conflict_group_id IS NOT NULL);


--
-- Name: mem0_memory_registry_delete_backlog_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mem0_memory_registry_delete_backlog_idx ON public.mem0_memory_registry USING btree (provider_delete_state, updated_at) WHERE (provider_delete_state = 'pending'::text);


--
-- Name: mem0_memory_registry_delete_scan_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mem0_memory_registry_delete_scan_idx ON public.mem0_memory_registry USING btree (provider_delete_state) WHERE (provider_delete_state = ANY (ARRAY['pending'::text, 'failed'::text]));


--
-- Name: mem0_memory_registry_reconsolidate_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mem0_memory_registry_reconsolidate_idx ON public.mem0_memory_registry USING btree (user_id, last_reconsolidated_at NULLS FIRST) WHERE (semantic_status = ANY (ARRAY['active'::text, 'ambiguous'::text]));


--
-- Name: mem0_memory_registry_searchable_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mem0_memory_registry_searchable_idx ON public.mem0_memory_registry USING btree (user_id, semantic_status, source_turn_seq) WHERE (semantic_status = ANY (ARRAY['active'::text, 'ambiguous'::text]));


--
-- Name: mem0_memory_sources_date_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mem0_memory_sources_date_idx ON public.mem0_memory_sources USING btree (user_id, source_activity_date);


--
-- Name: mem0_memory_sources_user_msg_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mem0_memory_sources_user_msg_idx ON public.mem0_memory_sources USING btree (user_id, source_message_id);


--
-- Name: memory_pipeline_states_lag_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_pipeline_states_lag_idx ON public.memory_pipeline_states USING btree (user_id) WHERE (consolidated_through_turn_seq < source_through_turn_seq);


--
-- Name: memory_pipeline_states_mode_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_pipeline_states_mode_idx ON public.memory_pipeline_states USING btree (mode, bootstrap_status);


--
-- Name: messages_user_actdate_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX messages_user_actdate_idx ON public.messages USING btree (user_id, activity_date);


--
-- Name: messages_user_id_id_sender_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX messages_user_id_id_sender_uq ON public.messages USING btree (user_id, id, sender);


--
-- Name: messages_user_id_id_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX messages_user_id_id_uq ON public.messages USING btree (user_id, id);


--
-- Name: messages_user_turn_position_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX messages_user_turn_position_uq ON public.messages USING btree (user_id, turn_seq, turn_position) WHERE (turn_seq IS NOT NULL);


--
-- Name: moly_life_ments_diary_date_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX moly_life_ments_diary_date_uq ON public.moly_life_ments USING btree (diary_date) WHERE (diary_date IS NOT NULL);


--
-- Name: order_items_order_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX order_items_order_idx ON public.order_items USING btree (order_id);


--
-- Name: order_items_product_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX order_items_product_idx ON public.order_items USING btree (product_id);


--
-- Name: orders_user_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX orders_user_created_idx ON public.orders USING btree (user_id, created_at DESC);


--
-- Name: payments_order_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payments_order_idx ON public.payments USING btree (order_id);


--
-- Name: payments_store_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payments_store_idx ON public.payments USING btree (store);


--
-- Name: payments_subscription_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payments_subscription_idx ON public.payments USING btree (subscription_id);


--
-- Name: payments_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payments_user_idx ON public.payments USING btree (user_id);


--
-- Name: privacy_ledger_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX privacy_ledger_user_idx ON public.privacy_ledger_events USING btree (user_id, id);


--
-- Name: privacy_subject_barriers_state_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX privacy_subject_barriers_state_idx ON public.privacy_subject_barriers USING btree (state);


--
-- Name: products_public_id_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX products_public_id_uq ON public.products USING btree (public_id) WHERE (public_id IS NOT NULL);


--
-- Name: relationship_events_replay_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX relationship_events_replay_idx ON public.relationship_events USING btree (user_id, activity_date, id);


--
-- Name: relationship_profile_renders_lookup_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX relationship_profile_renders_lookup_idx ON public.relationship_profile_renders USING btree (user_id, locale, prompt_revision DESC);


--
-- Name: revenuecat_events_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX revenuecat_events_status_idx ON public.revenuecat_events USING btree (status, received_at);


--
-- Name: revenuecat_events_status_next_attempt_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX revenuecat_events_status_next_attempt_idx ON public.revenuecat_events USING btree (status, next_attempt_at);


--
-- Name: reward_ad_sessions_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX reward_ad_sessions_user_idx ON public.reward_ad_sessions USING btree (user_id);


--
-- Name: routine_completions_routine_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX routine_completions_routine_idx ON public.routine_completions USING btree (routine_id);


--
-- Name: routine_completions_user_actdate_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX routine_completions_user_actdate_idx ON public.routine_completions USING btree (user_id, activity_date);


--
-- Name: routine_completions_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX routine_completions_user_idx ON public.routine_completions USING btree (user_id);


--
-- Name: routines_user_id_id_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX routines_user_id_id_uq ON public.routines USING btree (user_id, id);


--
-- Name: routines_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX routines_user_idx ON public.routines USING btree (user_id);


--
-- Name: shadow_prompt_traces_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX shadow_prompt_traces_user_idx ON public.shadow_prompt_traces USING btree (user_id, turn_seq);


--
-- Name: subscriptions_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX subscriptions_user_idx ON public.subscriptions USING btree (user_id);


--
-- Name: user_devices_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_devices_user_idx ON public.user_devices USING btree (user_id);


--
-- Name: user_interaction_contract_items_active_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_interaction_contract_items_active_idx ON public.user_interaction_contract_items USING btree (user_id, section) WHERE (status = 'active'::text);


--
-- Name: user_interaction_contracts_published_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX user_interaction_contracts_published_uq ON public.user_interaction_contracts USING btree (user_id, locale) WHERE (status = 'published'::text);


--
-- Name: user_items_order_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_items_order_idx ON public.user_items USING btree (order_id);


--
-- Name: user_items_product_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_items_product_idx ON public.user_items USING btree (product_id);


--
-- Name: user_items_user_equipped_slot_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX user_items_user_equipped_slot_uq ON public.user_items USING btree (user_id, equipped_slot) WHERE (equipped_slot IS NOT NULL);


--
-- Name: user_items_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_items_user_idx ON public.user_items USING btree (user_id);


--
-- Name: user_schedules_due_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_schedules_due_idx ON public.user_schedules USING btree (kind, next_due_at);


--
-- Name: moly_memories_v2_user_idx; Type: INDEX; Schema: vecs; Owner: -
--

CREATE INDEX moly_memories_v2_user_idx ON vecs.moly_memories_v2 USING btree (((metadata ->> 'user_id'::text)));


--
-- Name: chat_contexts chat_contexts_normalized_snapshot_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER chat_contexts_normalized_snapshot_guard BEFORE INSERT OR UPDATE ON public.chat_contexts FOR EACH ROW EXECUTE FUNCTION public.guard_normalized_memory_snapshot();


--
-- Name: orders orders_set_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER orders_set_updated_at BEFORE UPDATE ON public.orders FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: profiles profiles_create_privacy_barrier; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER profiles_create_privacy_barrier AFTER INSERT ON public.profiles FOR EACH ROW EXECUTE FUNCTION public.create_privacy_barrier_for_profile();


--
-- Name: profiles profiles_set_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER profiles_set_updated_at BEFORE UPDATE ON public.profiles FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: routines routines_set_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER routines_set_updated_at BEFORE UPDATE ON public.routines FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: subscriptions subscriptions_set_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER subscriptions_set_updated_at BEFORE UPDATE ON public.subscriptions FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: profiles trg_normalize_profile_language; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_normalize_profile_language BEFORE INSERT OR UPDATE OF language ON public.profiles FOR EACH ROW EXECUTE FUNCTION public.normalize_profile_language();


--
-- Name: ai_usage_ledger ai_usage_ledger_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_usage_ledger
    ADD CONSTRAINT ai_usage_ledger_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE SET NULL;


--
-- Name: async_jobs async_jobs_replay_of_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.async_jobs
    ADD CONSTRAINT async_jobs_replay_of_fkey FOREIGN KEY (replay_of) REFERENCES public.async_jobs(id);


--
-- Name: async_jobs async_jobs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.async_jobs
    ADD CONSTRAINT async_jobs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: chat_active_turns chat_active_turns_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_active_turns
    ADD CONSTRAINT chat_active_turns_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: chat_contexts chat_contexts_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_contexts
    ADD CONSTRAINT chat_contexts_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: chat_response_references chat_response_references_user_id_diary_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_response_references
    ADD CONSTRAINT chat_response_references_user_id_diary_id_fkey FOREIGN KEY (user_id, diary_id) REFERENCES public.diaries(user_id, id) ON DELETE RESTRICT;


--
-- Name: chat_response_references chat_response_references_user_id_reply_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_response_references
    ADD CONSTRAINT chat_response_references_user_id_reply_message_id_fkey FOREIGN KEY (user_id, reply_message_id) REFERENCES public.messages(user_id, id) ON DELETE CASCADE;


--
-- Name: conversation_checkpoints conversation_checkpoints_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_checkpoints
    ADD CONSTRAINT conversation_checkpoints_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: conversation_checkpoints conversation_checkpoints_user_message_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_checkpoints
    ADD CONSTRAINT conversation_checkpoints_user_message_fk FOREIGN KEY (user_id, through_message_id) REFERENCES public.messages(user_id, id) ON DELETE CASCADE;


--
-- Name: conversation_focus conversation_focus_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_focus
    ADD CONSTRAINT conversation_focus_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: diaries diaries_preset_ment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diaries
    ADD CONSTRAINT diaries_preset_ment_id_fkey FOREIGN KEY (preset_ment_id) REFERENCES public.moly_life_ments(id) ON DELETE SET NULL;


--
-- Name: diaries diaries_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diaries
    ADD CONSTRAINT diaries_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: diary_claim_sources diary_claim_sources_user_id_diary_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diary_claim_sources
    ADD CONSTRAINT diary_claim_sources_user_id_diary_id_fkey FOREIGN KEY (user_id, diary_id) REFERENCES public.diaries(user_id, id) ON DELETE CASCADE;


--
-- Name: diary_claim_sources diary_claim_sources_user_id_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diary_claim_sources
    ADD CONSTRAINT diary_claim_sources_user_id_message_id_fkey FOREIGN KEY (user_id, message_id) REFERENCES public.messages(user_id, id) ON DELETE CASCADE;


--
-- Name: diary_generation_results diary_generation_results_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diary_generation_results
    ADD CONSTRAINT diary_generation_results_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: diary_recall_documents diary_recall_documents_user_id_diary_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diary_recall_documents
    ADD CONSTRAINT diary_recall_documents_user_id_diary_id_fkey FOREIGN KEY (user_id, diary_id) REFERENCES public.diaries(user_id, id) ON DELETE CASCADE;


--
-- Name: feedback feedback_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feedback
    ADD CONSTRAINT feedback_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: greetings greetings_committed_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.greetings
    ADD CONSTRAINT greetings_committed_message_id_fkey FOREIGN KEY (committed_message_id) REFERENCES public.messages(id) ON DELETE SET NULL;


--
-- Name: greetings greetings_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.greetings
    ADD CONSTRAINT greetings_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: hay_transactions hay_transactions_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hay_transactions
    ADD CONSTRAINT hay_transactions_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id) ON DELETE SET NULL;


--
-- Name: hay_transactions hay_transactions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hay_transactions
    ADD CONSTRAINT hay_transactions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: idempotency_keys idempotency_keys_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.idempotency_keys
    ADD CONSTRAINT idempotency_keys_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: idempotency_keys idempotency_reply_message_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.idempotency_keys
    ADD CONSTRAINT idempotency_reply_message_fk FOREIGN KEY (user_id, reply_message_id) REFERENCES public.messages(user_id, id) ON DELETE CASCADE;


--
-- Name: job_attempts job_attempts_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_attempts
    ADD CONSTRAINT job_attempts_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.async_jobs(id) ON DELETE CASCADE;


--
-- Name: mem0_ingest_candidate_sources mem0_ingest_candidate_sources_candidate_id_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mem0_ingest_candidate_sources
    ADD CONSTRAINT mem0_ingest_candidate_sources_candidate_id_user_id_fkey FOREIGN KEY (candidate_id, user_id) REFERENCES public.mem0_ingest_candidates(id, user_id) ON DELETE CASCADE;


--
-- Name: mem0_ingest_candidate_sources mem0_ingest_candidate_sources_user_id_source_message_id_so_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mem0_ingest_candidate_sources
    ADD CONSTRAINT mem0_ingest_candidate_sources_user_id_source_message_id_so_fkey FOREIGN KEY (user_id, source_message_id, source_sender) REFERENCES public.messages(user_id, id, sender) ON DELETE CASCADE;


--
-- Name: mem0_ingest_candidates mem0_ingest_candidates_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mem0_ingest_candidates
    ADD CONSTRAINT mem0_ingest_candidates_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: mem0_memory_registry mem0_memory_registry_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mem0_memory_registry
    ADD CONSTRAINT mem0_memory_registry_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: mem0_memory_sources mem0_memory_sources_registry_id_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mem0_memory_sources
    ADD CONSTRAINT mem0_memory_sources_registry_id_user_id_fkey FOREIGN KEY (registry_id, user_id) REFERENCES public.mem0_memory_registry(id, user_id) ON DELETE CASCADE;


--
-- Name: mem0_memory_sources mem0_memory_sources_user_id_source_message_id_source_sende_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mem0_memory_sources
    ADD CONSTRAINT mem0_memory_sources_user_id_source_message_id_source_sende_fkey FOREIGN KEY (user_id, source_message_id, source_sender) REFERENCES public.messages(user_id, id, sender) ON DELETE CASCADE;


--
-- Name: memory_pipeline_states memory_pipeline_states_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_pipeline_states
    ADD CONSTRAINT memory_pipeline_states_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: messages messages_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: order_items order_items_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id) ON DELETE CASCADE;


--
-- Name: order_items order_items_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE RESTRICT;


--
-- Name: orders orders_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: payments payments_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id) ON DELETE CASCADE;


--
-- Name: payments payments_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id) ON DELETE CASCADE;


--
-- Name: payments payments_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: profiles profiles_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.profiles
    ADD CONSTRAINT profiles_id_fkey FOREIGN KEY (id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: relationship_events relationship_events_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relationship_events
    ADD CONSTRAINT relationship_events_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: relationship_profile_renders relationship_profile_renders_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relationship_profile_renders
    ADD CONSTRAINT relationship_profile_renders_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: reward_ad_sessions reward_ad_sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reward_ad_sessions
    ADD CONSTRAINT reward_ad_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: routine_completions routine_completions_routine_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.routine_completions
    ADD CONSTRAINT routine_completions_routine_id_fkey FOREIGN KEY (routine_id) REFERENCES public.routines(id) ON DELETE CASCADE;


--
-- Name: routine_completions routine_completions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.routine_completions
    ADD CONSTRAINT routine_completions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: routine_completions routine_completions_user_routine_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.routine_completions
    ADD CONSTRAINT routine_completions_user_routine_fk FOREIGN KEY (user_id, routine_id) REFERENCES public.routines(user_id, id) ON DELETE CASCADE;


--
-- Name: routines routines_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.routines
    ADD CONSTRAINT routines_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: shadow_prompt_traces shadow_prompt_traces_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shadow_prompt_traces
    ADD CONSTRAINT shadow_prompt_traces_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: subscription_hay_grants subscription_hay_grants_clawback_hay_transaction_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_hay_grants
    ADD CONSTRAINT subscription_hay_grants_clawback_hay_transaction_id_fkey FOREIGN KEY (clawback_hay_transaction_id) REFERENCES public.hay_transactions(id) ON DELETE SET NULL;


--
-- Name: subscription_hay_grants subscription_hay_grants_hay_transaction_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_hay_grants
    ADD CONSTRAINT subscription_hay_grants_hay_transaction_id_fkey FOREIGN KEY (hay_transaction_id) REFERENCES public.hay_transactions(id) ON DELETE SET NULL;


--
-- Name: subscription_hay_grants subscription_hay_grants_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_hay_grants
    ADD CONSTRAINT subscription_hay_grants_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: subscriptions subscriptions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: user_daily_stats user_daily_stats_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_daily_stats
    ADD CONSTRAINT user_daily_stats_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: user_devices user_devices_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_devices
    ADD CONSTRAINT user_devices_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: user_interaction_contract_items user_interaction_contract_items_contract_id_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_interaction_contract_items
    ADD CONSTRAINT user_interaction_contract_items_contract_id_user_id_fkey FOREIGN KEY (contract_id, user_id) REFERENCES public.user_interaction_contracts(id, user_id) ON DELETE CASCADE;


--
-- Name: user_interaction_contract_items user_interaction_contract_items_user_id_source_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_interaction_contract_items
    ADD CONSTRAINT user_interaction_contract_items_user_id_source_message_id_fkey FOREIGN KEY (user_id, source_message_id) REFERENCES public.messages(user_id, id) ON DELETE SET NULL (source_message_id);


--
-- Name: user_interaction_contracts user_interaction_contracts_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_interaction_contracts
    ADD CONSTRAINT user_interaction_contracts_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: user_items user_items_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_items
    ADD CONSTRAINT user_items_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id) ON DELETE SET NULL;


--
-- Name: user_items user_items_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_items
    ADD CONSTRAINT user_items_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;


--
-- Name: user_items user_items_product_slot_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_items
    ADD CONSTRAINT user_items_product_slot_fk FOREIGN KEY (product_id, equipped_slot) REFERENCES public.products(id, slot);


--
-- Name: user_items user_items_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_items
    ADD CONSTRAINT user_items_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: user_notification_settings user_notification_settings_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_notification_settings
    ADD CONSTRAINT user_notification_settings_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: user_relationship_states user_relationship_states_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_relationship_states
    ADD CONSTRAINT user_relationship_states_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: user_schedules user_schedules_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_schedules
    ADD CONSTRAINT user_schedules_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: ai_price_catalog; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.ai_price_catalog ENABLE ROW LEVEL SECURITY;

--
-- Name: ai_usage_ledger; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.ai_usage_ledger ENABLE ROW LEVEL SECURITY;

--
-- Name: app_config; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.app_config ENABLE ROW LEVEL SECURITY;

--
-- Name: async_jobs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.async_jobs ENABLE ROW LEVEL SECURITY;

--
-- Name: chat_active_turns; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.chat_active_turns ENABLE ROW LEVEL SECURITY;

--
-- Name: chat_contexts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.chat_contexts ENABLE ROW LEVEL SECURITY;

--
-- Name: chat_response_references; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.chat_response_references ENABLE ROW LEVEL SECURITY;

--
-- Name: conversation_checkpoints; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.conversation_checkpoints ENABLE ROW LEVEL SECURITY;

--
-- Name: conversation_focus; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.conversation_focus ENABLE ROW LEVEL SECURITY;

--
-- Name: diaries; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.diaries ENABLE ROW LEVEL SECURITY;

--
-- Name: diary_claim_sources; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.diary_claim_sources ENABLE ROW LEVEL SECURITY;

--
-- Name: diary_gen_claims; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.diary_gen_claims ENABLE ROW LEVEL SECURITY;

--
-- Name: diary_generation_results; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.diary_generation_results ENABLE ROW LEVEL SECURITY;

--
-- Name: diary_recall_documents; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.diary_recall_documents ENABLE ROW LEVEL SECURITY;

--
-- Name: feedback; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.feedback ENABLE ROW LEVEL SECURITY;

--
-- Name: greetings; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.greetings ENABLE ROW LEVEL SECURITY;

--
-- Name: hay_transactions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.hay_transactions ENABLE ROW LEVEL SECURITY;

--
-- Name: idempotency_keys; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.idempotency_keys ENABLE ROW LEVEL SECURITY;

--
-- Name: job_attempts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.job_attempts ENABLE ROW LEVEL SECURITY;

--
-- Name: mem0_ingest_candidate_sources; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.mem0_ingest_candidate_sources ENABLE ROW LEVEL SECURITY;

--
-- Name: mem0_ingest_candidates; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.mem0_ingest_candidates ENABLE ROW LEVEL SECURITY;

--
-- Name: mem0_memory_registry; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.mem0_memory_registry ENABLE ROW LEVEL SECURITY;

--
-- Name: mem0_memory_sources; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.mem0_memory_sources ENABLE ROW LEVEL SECURITY;

--
-- Name: memory_pipeline_states; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.memory_pipeline_states ENABLE ROW LEVEL SECURITY;

--
-- Name: messages; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.messages ENABLE ROW LEVEL SECURITY;

--
-- Name: moly_life_ments; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.moly_life_ments ENABLE ROW LEVEL SECURITY;

--
-- Name: order_items; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.order_items ENABLE ROW LEVEL SECURITY;

--
-- Name: orders; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.orders ENABLE ROW LEVEL SECURITY;

--
-- Name: payments; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.payments ENABLE ROW LEVEL SECURITY;

--
-- Name: privacy_ledger_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.privacy_ledger_events ENABLE ROW LEVEL SECURITY;

--
-- Name: privacy_subject_barriers; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.privacy_subject_barriers ENABLE ROW LEVEL SECURITY;

--
-- Name: products; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.products ENABLE ROW LEVEL SECURITY;

--
-- Name: profiles; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;

--
-- Name: provider_backoffs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.provider_backoffs ENABLE ROW LEVEL SECURITY;

--
-- Name: relationship_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.relationship_events ENABLE ROW LEVEL SECURITY;

--
-- Name: relationship_profile_renders; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.relationship_profile_renders ENABLE ROW LEVEL SECURITY;

--
-- Name: revenuecat_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.revenuecat_events ENABLE ROW LEVEL SECURITY;

--
-- Name: reward_ad_sessions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.reward_ad_sessions ENABLE ROW LEVEL SECURITY;

--
-- Name: routine_completions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.routine_completions ENABLE ROW LEVEL SECURITY;

--
-- Name: routines; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.routines ENABLE ROW LEVEL SECURITY;

--
-- Name: schema_migrations; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.schema_migrations ENABLE ROW LEVEL SECURITY;

--
-- Name: shadow_prompt_traces; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.shadow_prompt_traces ENABLE ROW LEVEL SECURITY;

--
-- Name: subscription_hay_grants; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.subscription_hay_grants ENABLE ROW LEVEL SECURITY;

--
-- Name: subscriptions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.subscriptions ENABLE ROW LEVEL SECURITY;

--
-- Name: user_daily_stats; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.user_daily_stats ENABLE ROW LEVEL SECURITY;

--
-- Name: user_devices; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.user_devices ENABLE ROW LEVEL SECURITY;

--
-- Name: user_interaction_contract_items; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.user_interaction_contract_items ENABLE ROW LEVEL SECURITY;

--
-- Name: user_interaction_contracts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.user_interaction_contracts ENABLE ROW LEVEL SECURITY;

--
-- Name: user_items; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.user_items ENABLE ROW LEVEL SECURITY;

--
-- Name: user_notification_settings; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.user_notification_settings ENABLE ROW LEVEL SECURITY;

--
-- Name: user_relationship_states; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.user_relationship_states ENABLE ROW LEVEL SECURITY;

--
-- Name: user_schedules; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.user_schedules ENABLE ROW LEVEL SECURITY;

--
-- Name: supabase_realtime; Type: PUBLICATION; Schema: -; Owner: -
--

CREATE PUBLICATION supabase_realtime WITH (publish = 'insert, update, delete, truncate');


--
-- Name: issue_graphql_placeholder; Type: EVENT TRIGGER; Schema: -; Owner: -
--

CREATE EVENT TRIGGER issue_graphql_placeholder ON sql_drop
         WHEN TAG IN ('DROP EXTENSION')
   EXECUTE FUNCTION extensions.set_graphql_placeholder();


--
-- Name: issue_pg_cron_access; Type: EVENT TRIGGER; Schema: -; Owner: -
--

CREATE EVENT TRIGGER issue_pg_cron_access ON ddl_command_end
         WHEN TAG IN ('CREATE EXTENSION')
   EXECUTE FUNCTION extensions.grant_pg_cron_access();


--
-- Name: issue_pg_graphql_access; Type: EVENT TRIGGER; Schema: -; Owner: -
--

CREATE EVENT TRIGGER issue_pg_graphql_access ON ddl_command_end
         WHEN TAG IN ('CREATE EXTENSION')
   EXECUTE FUNCTION extensions.grant_pg_graphql_access();


--
-- Name: issue_pg_net_access; Type: EVENT TRIGGER; Schema: -; Owner: -
--

CREATE EVENT TRIGGER issue_pg_net_access ON ddl_command_end
         WHEN TAG IN ('CREATE EXTENSION')
   EXECUTE FUNCTION extensions.grant_pg_net_access();


--
-- Name: pgrst_ddl_watch; Type: EVENT TRIGGER; Schema: -; Owner: -
--

CREATE EVENT TRIGGER pgrst_ddl_watch ON ddl_command_end
   EXECUTE FUNCTION extensions.pgrst_ddl_watch();


--
-- Name: pgrst_drop_watch; Type: EVENT TRIGGER; Schema: -; Owner: -
--

CREATE EVENT TRIGGER pgrst_drop_watch ON sql_drop
   EXECUTE FUNCTION extensions.pgrst_drop_watch();


--
-- PostgreSQL database dump complete
--

\unrestrict TnNnIYPqyStlVWWLurwYkyPhiMTZB0FnsVqS0mxBOgLR7HcfO9kJbltQEykFkAo

