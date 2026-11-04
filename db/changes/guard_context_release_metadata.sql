-- 최초 운영 전환 1/6: 짧은 metadata TX. 공개 대화/일반 worker를 유지한다.
-- db.apply --statement-timeout-ms 750으로 바깥 DO 시작 전 전체 DDL 제한을 설정한다.
-- 기존 테이블 잠금을 즉시 얻지 못하면 NOWAIT 실패로 전체 rollback한다.
-- CHECK는 NOT VALID로 추가만 하고 아래 별도 4개 검증 파일에서 각각 검증한다.
-- 성공 뒤 재실행하지 말고 중간 catalog와 CHECK validated 상태를 먼저 확인한다.
LOCK TABLE public.app_config, public.chat_contexts, public.conversation_checkpoints,
    public.mem0_ingest_candidates, public.mem0_memory_registry,
    public.mem0_memory_sources, public.memory_pipeline_states,
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

-- 준비 중에만 변경을 기록한다. 기존 자료·처리 커서·공개 설정은 바꾸지 않는다.
-- 최초 도입과 기존 online 개발 DB 추가에 같은 SQL을 쓴다. db.apply의 단일 TX로 적용한다.
CREATE TABLE public.retention_preparation_revisions (
    preparation_id uuid NOT NULL,
    user_id uuid NOT NULL,
    through_message_id bigint NOT NULL,
    revision bigint DEFAULT 0 NOT NULL,
    CONSTRAINT retention_preparation_revisions_pkey PRIMARY KEY (preparation_id,user_id),
    CONSTRAINT retention_preparation_revisions_preparation_id_fkey FOREIGN KEY (preparation_id) REFERENCES public.retention_preparations(id),
    CONSTRAINT retention_preparation_revisions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE,
    CONSTRAINT retention_preparation_revisions_through_check CHECK (through_message_id >= 0),
    CONSTRAINT retention_preparation_revisions_revision_check CHECK (revision >= 0)
);
CREATE INDEX retention_preparation_revisions_user_idx ON public.retention_preparation_revisions USING btree (user_id);
ALTER TABLE public.retention_preparation_revisions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.retention_preparation_revisions FROM PUBLIC,anon,authenticated,service_role;
GRANT ALL ON public.retention_preparation_revisions TO service_role;

-- 기술 metadata 행이며 사용자 기능 flag가 아니다. 기존 번호는 초기화하지 않는다.
INSERT INTO public.app_config (key,value)
VALUES ('context_retention_revision_fence','0'::jsonb) ON CONFLICT (key) DO NOTHING;

-- clock 경합은 source writer를 실패시키지 않고 추가 이벤트로 기록한다.
CREATE TABLE public.retention_preparation_revision_events (
    id bigserial NOT NULL,
    preparation_id uuid NOT NULL,
    user_id uuid NOT NULL,
    CONSTRAINT retention_preparation_revision_events_pkey PRIMARY KEY (id),
    CONSTRAINT retention_preparation_revision_events_preparation_id_fkey FOREIGN KEY (preparation_id) REFERENCES public.retention_preparations(id),
    CONSTRAINT retention_preparation_revision_events_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE
);
CREATE INDEX retention_preparation_revision_events_subject_idx ON public.retention_preparation_revision_events USING btree (preparation_id,user_id);
ALTER TABLE public.retention_preparation_revision_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.retention_preparation_revision_events FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON public.retention_preparation_revision_events TO service_role;
REVOKE ALL ON SEQUENCE public.retention_preparation_revision_events_id_seq FROM PUBLIC,anon,authenticated,service_role;

CREATE FUNCTION public.bump_retention_preparation_revision() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO ''
AS $function$
DECLARE
    old_row jsonb;
    new_row jsonb;
    old_owner uuid;
    new_owner uuid;
    owner_id uuid;
    ignored_change boolean := false;
    fence_value jsonb;
    clock_entry record;
    locked_through bigint;
    mutating boolean;
BEGIN
    SELECT value INTO fence_value FROM public.app_config
     WHERE key = 'context_retention_revision_fence' FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'retention_revision_fence_missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.retention_preparations
                   WHERE state IN ('building','prepared')) THEN
        RETURN NULL;
    END IF;
    IF TG_OP <> 'INSERT' THEN old_row := to_jsonb(OLD); END IF;
    IF TG_OP <> 'DELETE' THEN new_row := to_jsonb(NEW); END IF;
    IF TG_TABLE_NAME = 'profiles' THEN
        old_owner := (old_row->>'id')::uuid;
        new_owner := (new_row->>'id')::uuid;
    ELSE
        old_owner := (old_row->>'user_id')::uuid;
        new_owner := (new_row->>'user_id')::uuid;
    END IF;
    IF TG_OP = 'UPDATE' AND old_owner IS NOT DISTINCT FROM new_owner THEN
        IF TG_TABLE_NAME = 'profiles'
           AND old_row->'language' IS NOT DISTINCT FROM new_row->'language'
           AND old_row->'timezone' IS NOT DISTINCT FROM new_row->'timezone' THEN
            ignored_change := true;
        ELSIF TG_TABLE_NAME = 'chat_contexts'
           AND old_row->'memory_generation' IS NOT DISTINCT FROM new_row->'memory_generation' THEN
            ignored_change := true;
        ELSIF TG_TABLE_NAME = 'memory_pipeline_states'
           AND old_row->'mode' IS NOT DISTINCT FROM new_row->'mode'
           AND old_row->'bootstrap_status' IS NOT DISTINCT FROM new_row->'bootstrap_status'
           AND old_row->'privacy_epoch' IS NOT DISTINCT FROM new_row->'privacy_epoch'
           AND old_row->'repair_generation' IS NOT DISTINCT FROM new_row->'repair_generation'
           AND COALESCE((new_row->>'source_through_turn_seq')::bigint,0) >= COALESCE((old_row->>'source_through_turn_seq')::bigint,0)
           AND COALESCE((new_row->>'ingest_through_turn_seq')::bigint,0) >= COALESCE((old_row->>'ingest_through_turn_seq')::bigint,0)
           AND COALESCE((new_row->>'consolidated_through_turn_seq')::bigint,0) >= COALESCE((old_row->>'consolidated_through_turn_seq')::bigint,0) THEN
            ignored_change := true;
        ELSIF TG_TABLE_NAME = 'privacy_subject_barriers'
           AND old_row->'state' IS NOT DISTINCT FROM new_row->'state'
           AND old_row->'epoch' IS NOT DISTINCT FROM new_row->'epoch' THEN
            ignored_change := true;
        END IF;
    END IF;
    FOR owner_id IN SELECT DISTINCT owner FROM unnest(ARRAY[old_owner,new_owner]) AS owner
        WHERE owner IS NOT NULL ORDER BY owner
    LOOP
        -- A newly eligible/unregistered writer participates in the registry fence.
        -- This never enrolls it or changes the approved source boundary.
        PERFORM p.id FROM public.retention_preparations p
         WHERE p.state IN ('building','prepared') AND NOT ignored_change
           AND NOT EXISTS (SELECT 1 FROM public.retention_preparation_revisions r
                WHERE r.preparation_id = p.id AND r.user_id = owner_id)
           AND (TG_TABLE_NAME NOT IN ('conversation_checkpoints','retention_prepared_contracts')
                OR (old_owner = owner_id AND (old_row->>'preparation_id')::uuid = p.id)
                OR (new_owner = owner_id AND (new_row->>'preparation_id')::uuid = p.id))
         ORDER BY p.id FOR KEY SHARE OF p;
        FOR clock_entry IN
            SELECT r.preparation_id,r.through_message_id
              FROM public.retention_preparation_revisions r
              JOIN public.retention_preparations p ON p.id = r.preparation_id
             WHERE p.state IN ('building','prepared') AND r.user_id = owner_id
               AND (TG_TABLE_NAME NOT IN ('conversation_checkpoints','retention_prepared_contracts')
                    OR (old_owner = owner_id AND (old_row->>'preparation_id')::uuid = r.preparation_id)
                    OR (new_owner = owner_id AND (new_row->>'preparation_id')::uuid = r.preparation_id))
             ORDER BY r.preparation_id
        LOOP
            mutating := NOT ignored_change AND CASE WHEN TG_TABLE_NAME = 'messages' THEN
                COALESCE(old_owner = owner_id AND (old_row->>'id')::bigint <= clock_entry.through_message_id,false)
                OR COALESCE(new_owner = owner_id AND (new_row->>'id')::bigint <= clock_entry.through_message_id,false)
                ELSE true END;
            BEGIN
                IF mutating THEN
                    SELECT r.through_message_id INTO locked_through
                      FROM public.retention_preparation_revisions r
                     WHERE r.preparation_id = clock_entry.preparation_id AND r.user_id = owner_id
                     FOR UPDATE NOWAIT;
                ELSE
                    SELECT r.through_message_id INTO locked_through
                      FROM public.retention_preparation_revisions r
                     WHERE r.preparation_id = clock_entry.preparation_id AND r.user_id = owner_id
                     FOR SHARE NOWAIT;
                END IF;
                IF locked_through IS NULL THEN CONTINUE; END IF;
                mutating := NOT ignored_change AND CASE WHEN TG_TABLE_NAME = 'messages' THEN
                    COALESCE(old_owner = owner_id AND (old_row->>'id')::bigint <= locked_through,false)
                    OR COALESCE(new_owner = owner_id AND (new_row->>'id')::bigint <= locked_through,false)
                    ELSE true END;
                IF mutating THEN
                    -- A shared read can see a newly extended boundary. Upgrade
                    -- only NOWAIT; concurrent upgrades fall back to events.
                    PERFORM 1 FROM public.retention_preparation_revisions r
                     WHERE r.preparation_id = clock_entry.preparation_id AND r.user_id = owner_id
                     FOR UPDATE NOWAIT;
                    UPDATE public.retention_preparation_revisions r SET revision = r.revision + 1
                     WHERE r.preparation_id = clock_entry.preparation_id AND r.user_id = owner_id;
                END IF;
            EXCEPTION WHEN lock_not_available THEN
                -- Never wait for another writer's clock while owning source rows.
                -- Registry sharing orders both semantic events and ignored tails
                -- against promotion; the clock itself is not an event FK target.
                PERFORM p.id FROM public.retention_preparations p
                 WHERE p.id = clock_entry.preparation_id AND p.state IN ('building','prepared')
                 FOR KEY SHARE OF p;
                IF NOT FOUND THEN CONTINUE; END IF;
                SELECT r.through_message_id INTO locked_through
                  FROM public.retention_preparation_revisions r
                 WHERE r.preparation_id = clock_entry.preparation_id AND r.user_id = owner_id;
                mutating := locked_through IS NOT NULL AND NOT ignored_change
                    AND CASE WHEN TG_TABLE_NAME = 'messages' THEN
                        COALESCE(old_owner = owner_id AND (old_row->>'id')::bigint <= locked_through,false)
                        OR COALESCE(new_owner = owner_id AND (new_row->>'id')::bigint <= locked_through,false)
                        ELSE true END;
                IF mutating THEN
                    INSERT INTO public.retention_preparation_revision_events(preparation_id,user_id)
                    VALUES (clock_entry.preparation_id,owner_id);
                END IF;
            END;
        END LOOP;
    END LOOP;
    RETURN NULL;
END;
$function$;
REVOKE ALL ON FUNCTION public.bump_retention_preparation_revision() FROM PUBLIC,anon,authenticated,service_role;

CREATE TRIGGER profiles_retention_preparation_revision
AFTER INSERT OR UPDATE OR DELETE ON public.profiles
FOR EACH ROW EXECUTE FUNCTION public.bump_retention_preparation_revision();
CREATE TRIGGER chat_contexts_retention_preparation_revision
AFTER INSERT OR UPDATE OR DELETE ON public.chat_contexts
FOR EACH ROW EXECUTE FUNCTION public.bump_retention_preparation_revision();
CREATE TRIGGER privacy_subject_barriers_retention_preparation_revision
AFTER INSERT OR UPDATE OR DELETE ON public.privacy_subject_barriers
FOR EACH ROW EXECUTE FUNCTION public.bump_retention_preparation_revision();
CREATE TRIGGER memory_pipeline_states_retention_preparation_revision
AFTER INSERT OR UPDATE OR DELETE ON public.memory_pipeline_states
FOR EACH ROW EXECUTE FUNCTION public.bump_retention_preparation_revision();
CREATE TRIGGER messages_retention_preparation_revision
AFTER INSERT OR UPDATE OR DELETE ON public.messages
FOR EACH ROW EXECUTE FUNCTION public.bump_retention_preparation_revision();
CREATE TRIGGER mem0_memory_registry_retention_preparation_revision
AFTER INSERT OR UPDATE OR DELETE ON public.mem0_memory_registry
FOR EACH ROW EXECUTE FUNCTION public.bump_retention_preparation_revision();
CREATE TRIGGER mem0_memory_sources_retention_preparation_revision
AFTER INSERT OR UPDATE OR DELETE ON public.mem0_memory_sources
FOR EACH ROW EXECUTE FUNCTION public.bump_retention_preparation_revision();
CREATE TRIGGER conversation_checkpoints_retention_preparation_revision
AFTER INSERT OR UPDATE OR DELETE ON public.conversation_checkpoints
FOR EACH ROW EXECUTE FUNCTION public.bump_retention_preparation_revision();
CREATE TRIGGER retention_prepared_contracts_retention_preparation_revision
AFTER INSERT OR UPDATE OR DELETE ON public.retention_prepared_contracts
FOR EACH ROW EXECUTE FUNCTION public.bump_retention_preparation_revision();
CREATE TRIGGER mem0_ingest_candidates_retention_preparation_revision
AFTER INSERT OR UPDATE OR DELETE ON public.mem0_ingest_candidates
FOR EACH ROW EXECUTE FUNCTION public.bump_retention_preparation_revision();
