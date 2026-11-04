-- 최초 운영 전환 1/6: 짧은 metadata TX. 공개 대화/일반 worker를 유지한다.
-- db.apply --statement-timeout-ms 750으로 바깥 DO 시작 전 전체 DDL 제한을 설정한다.
-- 기존 테이블 잠금을 즉시 얻지 못하면 NOWAIT 실패로 전체 rollback한다.
-- CHECK는 NOT VALID로 추가만 하고 아래 별도 4개 검증 파일에서 각각 검증한다.
-- 성공 뒤 재실행하지 말고 중간 catalog와 CHECK validated 상태를 먼저 확인한다.
LOCK TABLE public.app_config, public.chat_contexts, public.conversation_checkpoints,
    public.mem0_ingest_candidates, public.mem0_memory_registry,
    public.messages, public.privacy_subject_barriers, public.profiles
    IN ACCESS EXCLUSIVE MODE NOWAIT;

DO $guardrails$
DECLARE
  existing_definition text;
  existing_kinds text[];
BEGIN
  SELECT pg_get_constraintdef(c.oid)
    INTO existing_definition
    FROM pg_constraint c
   WHERE c.conrelid = 'public.messages'::regclass
     AND c.conname = 'messages_kind_check'
     AND c.contype = 'c';
  IF existing_definition IS NULL THEN
    RAISE EXCEPTION 'messages_kind_check is missing; inspect the target schema first';
  END IF;
  SELECT array_agg(DISTINCT parts[1] ORDER BY parts[1])
    INTO existing_kinds
    FROM regexp_matches(existing_definition, '''([^'']+)''', 'g') AS parts;
  IF existing_kinds IS DISTINCT FROM ARRAY[
       'fortune_context_root', 'fortune_derived', 'greeting', 'normal', 'topic_opening'
     ]::text[]
     AND existing_kinds IS DISTINCT FROM ARRAY[
       'fortune_context_root', 'fortune_derived', 'greeting', 'normal',
       'safety', 'safety_check', 'topic_opening'
     ]::text[] THEN
    RAISE EXCEPTION 'Unexpected message kinds; preserve and review the existing constraint';
  END IF;
END
$guardrails$;

ALTER TABLE public.messages
  ADD CONSTRAINT messages_kind_guardrails_check
  CHECK (kind IN (
    'normal', 'greeting', 'fortune_context_root', 'fortune_derived', 'topic_opening',
    'safety_check', 'safety'
  )) NOT VALID;



ALTER TABLE public.conversation_checkpoints
  ADD COLUMN IF NOT EXISTS state_json jsonb,
  ADD COLUMN IF NOT EXISTS validation_json jsonb;
ALTER TABLE public.mem0_memory_registry
  ADD COLUMN IF NOT EXISTS canonical_text text,
  ADD COLUMN IF NOT EXISTS context_priority text NOT NULL DEFAULT 'related';
ALTER TABLE public.mem0_ingest_candidates
  ADD COLUMN IF NOT EXISTS context_priority text NOT NULL DEFAULT 'related';
DO $context_priority$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'public.mem0_memory_registry'::regclass
       AND conname = 'mem0_memory_registry_context_priority_check'
  ) THEN
    ALTER TABLE public.mem0_memory_registry
      ADD CONSTRAINT mem0_memory_registry_context_priority_check
      CHECK (context_priority IN ('related', 'required')) NOT VALID;
  END IF;
END
$context_priority$;

DO $candidate_priority$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'public.mem0_ingest_candidates'::regclass
       AND conname = 'mem0_ingest_candidates_context_priority_check'
  ) THEN
    ALTER TABLE public.mem0_ingest_candidates
      ADD CONSTRAINT mem0_ingest_candidates_context_priority_check
      CHECK (context_priority IN ('related', 'required')) NOT VALID;
  END IF;
END
$candidate_priority$;

ALTER TABLE public.chat_contexts
  ADD COLUMN IF NOT EXISTS contract_processed_through_message_id bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS contract_generation bigint NOT NULL DEFAULT 0;



ALTER TABLE public.chat_contexts
  ADD COLUMN IF NOT EXISTS retention_pressure_through_message_id bigint;
DO $pressure_constraint$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
    WHERE conrelid='public.chat_contexts'::regclass
      AND conname='chat_contexts_retention_pressure_positive_check') THEN
    ALTER TABLE public.chat_contexts
      ADD CONSTRAINT chat_contexts_retention_pressure_positive_check
      CHECK (retention_pressure_through_message_id IS NULL OR retention_pressure_through_message_id>0)
      NOT VALID;
  END IF;
END
$pressure_constraint$;


CREATE OR REPLACE FUNCTION public.reset_retention_pressure_generation() RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $function$
BEGIN
  IF NEW.memory_generation IS DISTINCT FROM OLD.memory_generation THEN
    NEW.retention_pressure_through_message_id := NULL;
  END IF;
  RETURN NEW;
END;
$function$;
REVOKE ALL ON FUNCTION public.reset_retention_pressure_generation() FROM PUBLIC, anon, authenticated, service_role;
DROP TRIGGER IF EXISTS chat_contexts_retention_pressure_generation ON public.chat_contexts;
CREATE TRIGGER chat_contexts_retention_pressure_generation BEFORE UPDATE OF memory_generation ON public.chat_contexts
FOR EACH ROW EXECUTE FUNCTION public.reset_retention_pressure_generation();

CREATE OR REPLACE FUNCTION public.reset_retention_pressure_privacy() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO ''
AS $function$
BEGIN
  IF NEW.state <> 'active' OR (TG_OP = 'UPDATE' AND NEW.epoch IS DISTINCT FROM OLD.epoch) THEN
    UPDATE public.chat_contexts SET retention_pressure_through_message_id = NULL
    WHERE user_id = NEW.user_id AND retention_pressure_through_message_id IS NOT NULL;
  END IF;
  RETURN NEW;
END;
$function$;
REVOKE ALL ON FUNCTION public.reset_retention_pressure_privacy() FROM PUBLIC, anon, authenticated, service_role;
DROP TRIGGER IF EXISTS privacy_subject_barriers_retention_pressure ON public.privacy_subject_barriers;
CREATE TRIGGER privacy_subject_barriers_retention_pressure AFTER INSERT OR UPDATE OF state, epoch ON public.privacy_subject_barriers
FOR EACH ROW EXECUTE FUNCTION public.reset_retention_pressure_privacy();


-- 무중단 준비 자료는 공개 계약/기존 checkpoint와 분리한다.
-- db.apply의 단일 TX/짧은 잠금 제한으로 적용하며 기존 자료는 변경하지 않는다.
-- 복구: 새 기능을 끄고 준비 registry를 aborted로 전환한다. 공개/기존 자료를 지우지 않는다.
CREATE TABLE public.retention_preparations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    state text DEFAULT 'building'::text NOT NULL,
    code_sha text NOT NULL,
    manifest_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT retention_preparations_pkey PRIMARY KEY (id),
    CONSTRAINT retention_preparations_state_check CHECK (state IN ('building', 'prepared', 'active', 'aborted')),
    CONSTRAINT retention_preparations_code_sha_check CHECK (code_sha ~ '^[0-9a-f]{40}$'),
    CONSTRAINT retention_preparations_manifest_check CHECK (jsonb_typeof(manifest_json) = 'object')
);
CREATE UNIQUE INDEX retention_preparations_one_active_uq ON public.retention_preparations USING btree (state) WHERE state = 'active';
ALTER TABLE public.retention_preparations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.retention_preparations FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON public.retention_preparations TO service_role;

CREATE TABLE public.retention_prepared_contracts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    preparation_id uuid NOT NULL,
    user_id uuid NOT NULL,
    memory_generation bigint NOT NULL,
    locale text NOT NULL,
    version bigint NOT NULL,
    document_json jsonb NOT NULL,
    validation_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    rendered_text text NOT NULL,
    render_hash text NOT NULL,
    status text DEFAULT 'ready'::text NOT NULL,
    processed_through_message_id bigint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    published_at timestamp with time zone,
    CONSTRAINT retention_prepared_contracts_pkey PRIMARY KEY (id),
    CONSTRAINT retention_prepared_contracts_preparation_id_fkey FOREIGN KEY (preparation_id) REFERENCES public.retention_preparations(id),
    CONSTRAINT retention_prepared_contracts_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE,
    CONSTRAINT retention_prepared_contracts_generation_check CHECK (memory_generation >= 0),
    CONSTRAINT retention_prepared_contracts_locale_check CHECK (locale IN ('ko', 'en', 'ja')),
    CONSTRAINT retention_prepared_contracts_version_check CHECK (version > 0),
    CONSTRAINT retention_prepared_contracts_render_hash_check CHECK (render_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT retention_prepared_contracts_status_check CHECK (status IN ('ready', 'published', 'superseded', 'draft')),
    CONSTRAINT retention_prepared_contracts_validation_check CHECK (jsonb_typeof(validation_json) = 'object'),
    CONSTRAINT retention_prepared_contracts_through_check CHECK (processed_through_message_id >= 0),
    CONSTRAINT retention_prepared_contracts_version_uq UNIQUE (preparation_id, user_id, memory_generation, locale, version)
);
CREATE UNIQUE INDEX retention_prepared_contracts_live_uq ON public.retention_prepared_contracts USING btree (preparation_id, user_id, memory_generation, locale) WHERE status IN ('ready', 'published');
ALTER TABLE public.retention_prepared_contracts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.retention_prepared_contracts FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON public.retention_prepared_contracts TO service_role;

ALTER TABLE public.conversation_checkpoints ADD COLUMN preparation_id uuid;
ALTER TABLE public.conversation_checkpoints ADD CONSTRAINT conversation_checkpoints_preparation_id_fkey
    FOREIGN KEY (preparation_id) REFERENCES public.retention_preparations(id);

-- 이미 online 구조가 있는 개발 DB용 좁은 호환 보완이다.
-- 기존 공개 행은 보존하고 NULL 준비 영역과 각 준비 ID의 게시 경계를 분리한다.
-- 짧은 잠금/전체 단일 TX 제한에서 실패하면 기존 index까지 함께 rollback한다.
DROP INDEX public.conversation_checkpoints_published_window_uq;
CREATE UNIQUE INDEX conversation_checkpoints_published_window_uq
    ON public.conversation_checkpoints USING btree (user_id, coverage_through_message_id)
    WHERE kind = 'window' AND publish_state = 'published' AND preparation_id IS NULL;
CREATE UNIQUE INDEX conversation_checkpoints_prepared_window_uq
    ON public.conversation_checkpoints USING btree (preparation_id, user_id, coverage_through_message_id)
    WHERE kind = 'window' AND publish_state = 'published' AND preparation_id IS NOT NULL;

-- 기존 설정은 덮어쓰지 않고 전역 전환용 행 잠금을 항상 잡을 수 있게 한다.
INSERT INTO public.app_config (key, value) VALUES ('context_retention_active_preparation', 'null'::jsonb)
ON CONFLICT (key) DO NOTHING;
