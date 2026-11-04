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
