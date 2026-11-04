"""Generated catalog contract for schema.sql; production checks are read-only."""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from urllib.parse import urlparse

import asyncpg

from db.catalog import compare_catalog, read_catalog

SCHEMA = Path(__file__).with_name('schema.sql')
CONTRACT = Path(__file__).with_name('schema_contract.json')
SUBSCRIPTION_SQL = Path(__file__).with_name('changes') / 'subscription_refund_access.sql'
SUBSCRIPTION_COLUMNS = ('public.subscriptions.access_evidence', 'public.revenuecat_events.processing_state')


def subscription_schema_upgrade(baseline: str) -> str:
    upgraded = baseline
    for old, new in (
        ('CREATE TABLE public.revenuecat_events (\n    event_id text NOT NULL,\n    payload jsonb NOT NULL,\n',
         'CREATE TABLE public.revenuecat_events (\n    event_id text NOT NULL,\n    payload jsonb NOT NULL,\n    processing_state jsonb,\n'),
        ('    last_event_at timestamp with time zone,\n    CONSTRAINT subscriptions_plan_check',
         '    last_event_at timestamp with time zone,\n    access_evidence jsonb,\n    CONSTRAINT subscriptions_plan_check'),
    ):
        if upgraded.count(old) != 1:
            raise ValueError('unexpected production subscription schema baseline')
        upgraded = upgraded.replace(old, new, 1)
    return upgraded


async def refresh_subscription_from_dev(*, env: str, expected_project_ref: str, reviewed_sql: Path,
                                        expected_sha256: str, baseline_schema: Path) -> list[str]:
    """Derive only two reviewed columns from dev; preserve the MAIN catalog in full.

    Dev and main intentionally differ in other features. Never import the dev
    catalog wholesale or certify those differences as production changes.
    """
    from db.envfile import DEV_PROJECT_REFS, assert_dev_target, load_conn, project_ref

    if env != 'dev' or expected_project_ref not in DEV_PROJECT_REFS:
        raise ValueError('explicit confirmed development source required')
    dsn = load_conn(env)
    assert_dev_target(env, dsn)
    target = urlparse(dsn)
    if (target.path != '/postgres' or target.port not in {None, 5432, 6543}
            or not ((target.hostname or '').endswith('.pooler.supabase.com')
                    or target.hostname == f'db.{expected_project_ref}.supabase.co')):
        raise ValueError('confirmed development connection endpoint required')
    if project_ref(dsn) != expected_project_ref or reviewed_sql.resolve() != SUBSCRIPTION_SQL.resolve():
        raise ValueError('reviewed subscription source mismatch')
    sql = reviewed_sql.read_bytes()
    if not re.fullmatch(r'[0-9a-f]{64}', expected_sha256) or hashlib.sha256(sql).hexdigest() != expected_sha256:
        raise ValueError('reviewed subscription SQL checksum mismatch')
    original_contract, source = CONTRACT.read_bytes(), SCHEMA.read_bytes()
    baseline = baseline_schema.read_bytes()
    contract = json.loads(original_contract)
    if hashlib.sha256(baseline).hexdigest() != contract['schema_sha256']:
        raise ValueError('production baseline checksum mismatch')
    if subscription_schema_upgrade(baseline.decode()).encode() != source:
        raise ValueError('schema contains changes outside subscription release')
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=10, command_timeout=15)
    try:
        actual = await read_catalog(conn)  # Owns a repeatable-read, read-only transaction.
    finally:
        await conn.close()
    expected_column = {'type': 'jsonb', 'not_null': False, 'default': None,
                       'identity': '', 'generated': '', 'collation': None}
    result = deepcopy(contract['catalog'])
    for key in SUBSCRIPTION_COLUMNS:
        if key in result['columns'] or actual['columns'].get(key) != expected_column:
            return ['reviewed nullable JSONB column mismatch: ' + key]
        result['columns'][key] = deepcopy(actual['columns'][key])
    generated = json.dumps({'schema_sha256': hashlib.sha256(source).hexdigest(), 'catalog': result},
                           ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    if (CONTRACT.read_bytes() != original_contract or SCHEMA.read_bytes() != source
            or reviewed_sql.read_bytes() != sql or baseline_schema.read_bytes() != baseline):
        raise ValueError('reviewed release inputs changed during metadata inspection')
    temporary = CONTRACT.with_name('.subscription-contract-' + uuid.uuid4().hex)
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            stream.write(generated)
        os.replace(temporary, CONTRACT)
    finally:
        temporary.unlink(missing_ok=True)
    return []



# Guard/context manifests augment the main catalog; unrelated release helpers remain intact.
REVIEWED_DEV_SQL = Path(__file__).with_name('changes') / 'crisis_guardrails.sql'


REVIEWED_GUARD_CONTEXT_SQL = Path(__file__).with_name('changes') / 'guard_context_v2.sql'
REVIEWED_ONLINE_SQL = Path(__file__).with_name('changes') / 'guard_context_online.sql'
REVIEWED_ONLINE_PROOF_SQL = Path(__file__).with_name('changes') / 'guard_context_online_proof.sql'
REVIEWED_ONLINE_INDEXES_SQL = Path(__file__).with_name('changes') / 'guard_context_online_indexes.sql'
REVIEWED_ONLINE_REVISIONS_SQL = Path(__file__).with_name('changes') / 'guard_context_online_revisions.sql'
REVIEWED_ONLINE_REVISION_FENCE_SQL = Path(__file__).with_name('changes') / 'guard_context_online_revision_fence.sql'
REVIEWED_ONLINE_REVISION_EVENTS_SQL = Path(__file__).with_name('changes') / 'guard_context_online_revision_events.sql'


def _column(sql_type: str, *, not_null: bool = False, default: str | None = None) -> dict:
    return {
        'type': sql_type, 'not_null': not_null, 'default': default,
        'identity': '', 'generated': '', 'collation': '"default"' if sql_type == 'text' else None,
    }


DEV_REFRESH_COLUMNS = {
    'public.chat_contexts.contract_processed_through_message_id':
        _column('bigint', not_null=True, default='0'),
    'public.chat_contexts.contract_generation': _column('bigint', not_null=True, default='0'),
    'public.conversation_checkpoints.state_json': _column('jsonb'),
    'public.conversation_checkpoints.validation_json': _column('jsonb'),
    'public.mem0_memory_registry.canonical_text': _column('text'),
    'public.mem0_memory_registry.context_priority':
        _column('text', not_null=True, default="'related'::text"),
    'public.mem0_ingest_candidates.context_priority':
        _column('text', not_null=True, default="'related'::text"),
}


_OLD_KINDS = (
    "'normal'::text, 'greeting'::text, 'fortune_context_root'::text, "
    "'fortune_derived'::text, 'topic_opening'::text"
)


_NEW_KINDS = _OLD_KINDS + ", 'safety_check'::text, 'safety'::text"


_OLD_KIND_DEFINITION = f'CHECK ((kind = ANY (ARRAY[{_OLD_KINDS}])))'


_NEW_KIND_DEFINITION = f'CHECK ((kind = ANY (ARRAY[{_NEW_KINDS}])))'


_PRIORITY_DEFINITION = "CHECK ((context_priority = ANY (ARRAY['related'::text, 'required'::text])))"


def _constraint(definition: str) -> dict:
    return {'definition': definition, 'validated': True, 'deferrable': False,
            'deferred': False, 'enforcement_triggers': []}


DEV_REFRESH_CONSTRAINTS = {
    'public.messages.messages_kind_check': _constraint(_NEW_KIND_DEFINITION),
    'public.mem0_memory_registry.mem0_memory_registry_context_priority_check':
        _constraint(_PRIORITY_DEFINITION),
    'public.mem0_ingest_candidates.mem0_ingest_candidates_context_priority_check':
        _constraint(_PRIORITY_DEFINITION),
}


def reviewed_schema_upgrade(baseline: str) -> str:
    """Apply only this release's exact source edits to the hash-verified baseline."""
    edits = [
        ('    checkpoint_source_hash text,\n',
         '    checkpoint_source_hash text,\n'
         '    contract_processed_through_message_id bigint DEFAULT 0 NOT NULL,\n'
         '    contract_generation bigint DEFAULT 0 NOT NULL,\n'),
        ("    publish_state text DEFAULT 'published'::text NOT NULL,\n",
         "    publish_state text DEFAULT 'published'::text NOT NULL,\n"
         '    state_json jsonb,\n    validation_json jsonb,\n'),
        ('    last_reconsolidated_at timestamp with time zone,\n',
         '    last_reconsolidated_at timestamp with time zone,\n'
         '    canonical_text text,\n'
         "    context_priority text DEFAULT 'related'::text NOT NULL,\n"
         '    CONSTRAINT mem0_memory_registry_context_priority_check '
         + _PRIORITY_DEFINITION + ',\n'),
        ('    category text,\n    CONSTRAINT mem0_ingest_candidates_status_check',
         "    category text,\n    context_priority text DEFAULT 'related'::text NOT NULL,\n"
         '    CONSTRAINT mem0_ingest_candidates_context_priority_check '
         + _PRIORITY_DEFINITION + ',\n    CONSTRAINT mem0_ingest_candidates_status_check'),
        ('    CONSTRAINT messages_kind_check ' + _OLD_KIND_DEFINITION + ',\n',
         '    CONSTRAINT messages_kind_check ' + _NEW_KIND_DEFINITION + ',\n'),
    ]
    upgraded = baseline
    for old, new in edits:
        if new in upgraded:
            # Idempotent re-check of an artifact already generated by this mode.
            if upgraded.count(new) != 1:
                raise ValueError('ambiguous reviewed schema edit')
            continue
        if upgraded.count(old) != 1:
            raise ValueError('baseline does not match this reviewed release')
        upgraded = upgraded.replace(old, new, 1)
    return upgraded


def reviewed_catalog_upgrade(baseline: dict) -> dict:
    """Preserve the baseline, changing only explicitly reviewed object definitions."""
    expected = deepcopy(baseline)
    previous = expected['constraints'].get('public.messages.messages_kind_check')
    if previous not in (_constraint(_OLD_KIND_DEFINITION), _constraint(_NEW_KIND_DEFINITION)):
        raise ValueError('unexpected baseline message constraint')
    for section, changes in (
        ('columns', DEV_REFRESH_COLUMNS), ('constraints', DEV_REFRESH_CONSTRAINTS),
    ):
        for key, definition in changes.items():
            if key.rsplit('.', 1)[0] not in expected['tables']:
                raise ValueError('reviewed object table is missing')
            if (key in expected[section] and key != 'public.messages.messages_kind_check'
                    and expected[section][key] != definition):
                raise ValueError('baseline has a conflicting reviewed object')
            expected[section][key] = deepcopy(definition)
    return expected


_PRESSURE_CHECK = 'CHECK (((retention_pressure_through_message_id IS NULL) OR (retention_pressure_through_message_id > 0)))'


_PRESSURE_FUNCTIONS = {
    'reset_retention_pressure_generation': (False, """BEGIN
  IF NEW.memory_generation IS DISTINCT FROM OLD.memory_generation THEN
    NEW.retention_pressure_through_message_id := NULL;
  END IF;
  RETURN NEW;
END;
"""),
    'reset_retention_pressure_privacy': (True, """BEGIN
  IF NEW.state <> 'active' OR (TG_OP = 'UPDATE' AND NEW.epoch IS DISTINCT FROM OLD.epoch) THEN
    UPDATE public.chat_contexts SET retention_pressure_through_message_id = NULL
    WHERE user_id = NEW.user_id AND retention_pressure_through_message_id IS NOT NULL;
  END IF;
  RETURN NEW;
END;
"""),
}


_PRESSURE_TRIGGERS = {
    'public.chat_contexts.chat_contexts_retention_pressure_generation':
        'CREATE TRIGGER chat_contexts_retention_pressure_generation BEFORE UPDATE OF memory_generation ON public.chat_contexts FOR EACH ROW EXECUTE FUNCTION public.reset_retention_pressure_generation()',
    'public.privacy_subject_barriers.privacy_subject_barriers_retention_pressure':
        'CREATE TRIGGER privacy_subject_barriers_retention_pressure AFTER INSERT OR UPDATE OF state, epoch ON public.privacy_subject_barriers FOR EACH ROW EXECUTE FUNCTION public.reset_retention_pressure_privacy()',
}


def reviewed_guard_context_schema_upgrade(baseline: str) -> str:
    functions = ''.join(
        f'-- FUNCTION: public.{name}()\nCREATE FUNCTION public.{name}() RETURNS trigger\n'
        f"    LANGUAGE plpgsql{' SECURITY DEFINER' if secure else ''}\n    SET search_path TO ''\n"
        f'    AS $function$\n{body}$function$;\n\n'
        for name, (secure, body) in _PRESSURE_FUNCTIONS.items())
    triggers = ''.join(
        f"-- TRIGGER: {key.rsplit('.', 1)[0]} {key.rsplit('.', 1)[1]}\n{definition};\n\n"
        for key, definition in _PRESSURE_TRIGGERS.items())
    grants = ''.join(
        f'-- ACL: public.FUNCTION {name}()\n'
        f'REVOKE ALL ON FUNCTION public.{name}() FROM PUBLIC, anon, authenticated, service_role;\n\n'
        for name in _PRESSURE_FUNCTIONS)
    edits = (
        ('    contract_generation bigint DEFAULT 0 NOT NULL,\n',
         '    contract_generation bigint DEFAULT 0 NOT NULL,\n'
         '    retention_pressure_through_message_id bigint,\n'
         '    CONSTRAINT chat_contexts_retention_pressure_positive_check ' + _PRESSURE_CHECK + ',\n'),
        ('-- FUNCTION: public.guard_normalized_memory_snapshot()\n', functions + '-- FUNCTION: public.guard_normalized_memory_snapshot()\n'),
        ('-- TRIGGER: public.orders orders_set_updated_at\n', triggers + '-- TRIGGER: public.orders orders_set_updated_at\n'),
        ('-- ACL: public.FUNCTION guard_normalized_memory_snapshot()\n', grants + '-- ACL: public.FUNCTION guard_normalized_memory_snapshot()\n'),
    )
    result = baseline
    for old, new in edits:
        if new in result:
            if result.count(new) != 1:
                raise ValueError('ambiguous guard context schema edit')
            continue
        if result.count(old) != 1:
            raise ValueError('baseline does not match guard context release')
        result = result.replace(old, new, 1)
    return result


def reviewed_guard_context_catalog_upgrade(baseline: dict) -> dict:
    expected = deepcopy(baseline)
    changes = {
        'columns': {'public.chat_contexts.retention_pressure_through_message_id': _column('bigint')},
        'constraints': {'public.chat_contexts.chat_contexts_retention_pressure_positive_check': _constraint(_PRESSURE_CHECK)},
        'triggers': {key: {'definition': definition, 'enabled': 'O'}
                     for key, definition in _PRESSURE_TRIGGERS.items()},
        'functions': {f'public.{name}()': {
            'definition': f'CREATE OR REPLACE FUNCTION public.{name}()\n RETURNS trigger\n'
                f" LANGUAGE plpgsql\n{' SECURITY DEFINER' + chr(10) if secure else ''} SET search_path TO ''\n"
                f'AS $function$\n{body}$function$\n',
            'owner': baseline['tables']['public.chat_contexts']['owner'],
        } for name, (secure, body) in _PRESSURE_FUNCTIONS.items()},
    }
    for section, entries in changes.items():
        for key, definition in entries.items():
            if key in expected[section] and expected[section][key] != definition:
                raise ValueError('conflicting guard context release object')
            expected[section][key] = definition
    # New trigger helpers have no direct execution grant; preserve all existing ACLs.
    for key in expected['function_grants']:
        if any(key.startswith(f'public.{name}():') for name in _PRESSURE_FUNCTIONS):
            raise ValueError('unexpected guard context function grant')
    return expected


_ONLINE_SCHEMA_SUFFIX = """
-- 무중단 가드레일/맥락 준비용 additive 구조.
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
"""


def reviewed_online_schema_upgrade(baseline: str) -> str:
    """Add only the reviewed online preparation structures; retain all prior SQL."""
    if _ONLINE_SCHEMA_SUFFIX in baseline:
        if baseline.count(_ONLINE_SCHEMA_SUFFIX) != 1:
            raise ValueError('ambiguous online preparation schema')
        return baseline
    if baseline.count('\nCOMMIT;\n') != 1:
        raise ValueError('unexpected online preparation schema baseline')
    return baseline.replace('\nCOMMIT;\n', _ONLINE_SCHEMA_SUFFIX + '\nCOMMIT;\n', 1)


def reviewed_online_catalog_upgrade(baseline: dict) -> dict:
    """Exact additive manifest, never a replacement with the whole dev catalog."""
    expected = deepcopy(baseline)
    registry = 'public.retention_preparations'
    contracts = 'public.retention_prepared_contracts'
    changes = {section: {} for section in baseline}
    table = deepcopy(baseline['tables']['public.chat_active_turns'])
    for name in (registry, contracts):
        changes['tables'][name] = deepcopy(table)
        for key, definition in baseline['relation_grants'].items():
            if key.startswith('public.chat_active_turns:'):
                changes['relation_grants'][name + key[len('public.chat_active_turns'):]] = deepcopy(definition)
    columns = {
        registry: {
            'id': _column('uuid', not_null=True, default='gen_random_uuid()'),
            'state': _column('text', not_null=True, default="'building'::text"),
            'code_sha': _column('text', not_null=True),
            'manifest_json': _column('jsonb', not_null=True, default="'{}'::jsonb"),
            'created_at': _column('timestamp with time zone', not_null=True, default='now()'),
            'updated_at': _column('timestamp with time zone', not_null=True, default='now()'),
        },
        contracts: {
            'id': _column('uuid', not_null=True, default='gen_random_uuid()'),
            'preparation_id': _column('uuid', not_null=True),
            'user_id': _column('uuid', not_null=True),
            'memory_generation': _column('bigint', not_null=True),
            'locale': _column('text', not_null=True),
            'version': _column('bigint', not_null=True),
            'document_json': _column('jsonb', not_null=True),
            'validation_json': _column('jsonb', not_null=True, default="'{}'::jsonb"),
            'rendered_text': _column('text', not_null=True),
            'render_hash': _column('text', not_null=True),
            'status': _column('text', not_null=True, default="'ready'::text"),
            'processed_through_message_id': _column('bigint', not_null=True, default='0'),
            'created_at': _column('timestamp with time zone', not_null=True, default='now()'),
            'published_at': _column('timestamp with time zone'),
        },
        'public.conversation_checkpoints': {'preparation_id': _column('uuid')},
    }
    for name, fields in columns.items():
        changes['columns'].update({f'{name}.{field}': value for field, value in fields.items()})
    constraints = {
        registry: {
            'retention_preparations_pkey': 'PRIMARY KEY (id)',
            'retention_preparations_state_check': "CHECK ((state = ANY (ARRAY['building'::text, 'prepared'::text, 'active'::text, 'aborted'::text])))",
            'retention_preparations_code_sha_check': "CHECK ((code_sha ~ '^[0-9a-f]{40}$'::text))",
            'retention_preparations_manifest_check': "CHECK ((jsonb_typeof(manifest_json) = 'object'::text))",
        },
        contracts: {
            'retention_prepared_contracts_pkey': 'PRIMARY KEY (id)',
            'retention_prepared_contracts_generation_check': 'CHECK ((memory_generation >= 0))',
            'retention_prepared_contracts_locale_check': "CHECK ((locale = ANY (ARRAY['ko'::text, 'en'::text, 'ja'::text])))",
            'retention_prepared_contracts_version_check': 'CHECK ((version > 0))',
            'retention_prepared_contracts_render_hash_check': "CHECK ((render_hash ~ '^[0-9a-f]{64}$'::text))",
            'retention_prepared_contracts_status_check': "CHECK ((status = ANY (ARRAY['ready'::text, 'published'::text, 'superseded'::text, 'draft'::text])))",
            'retention_prepared_contracts_validation_check': "CHECK ((jsonb_typeof(validation_json) = 'object'::text))",
            'retention_prepared_contracts_through_check': 'CHECK ((processed_through_message_id >= 0))',
            'retention_prepared_contracts_version_uq': 'UNIQUE (preparation_id, user_id, memory_generation, locale, version)',
        },
    }
    for name, entries in constraints.items():
        changes['constraints'].update({f'{name}.{key}': _constraint(value) for key, value in entries.items()})
    for name, key, column, target, cascade in (
        (contracts, 'retention_prepared_contracts_preparation_id_fkey', 'preparation_id', registry, False),
        (contracts, 'retention_prepared_contracts_user_id_fkey', 'user_id', 'public.profiles', True),
        ('public.conversation_checkpoints', 'conversation_checkpoints_preparation_id_fkey', 'preparation_id', registry, False),
    ):
        definition = _constraint(f'FOREIGN KEY ({column}) REFERENCES {target}(id)' + (' ON DELETE CASCADE' if cascade else ''))
        definition['enforcement_triggers'] = sorted([f'{name}:17:O', f'{name}:5:O', f'{target}:17:O', f'{target}:9:O'])
        changes['constraints'][f'{name}.{key}'] = definition
    for name, key, fields, where in (
        (registry, 'retention_preparations_pkey', 'id', ''),
        (registry, 'retention_preparations_one_active_uq', 'state', " WHERE (state = 'active'::text)"),
        (contracts, 'retention_prepared_contracts_pkey', 'id', ''),
        (contracts, 'retention_prepared_contracts_version_uq', 'preparation_id, user_id, memory_generation, locale, version', ''),
        (contracts, 'retention_prepared_contracts_live_uq', 'preparation_id, user_id, memory_generation, locale', " WHERE (status = ANY (ARRAY['ready'::text, 'published'::text]))"),
    ):
        changes['indexes'][f'public.{key}'] = {
            'definition': f'CREATE UNIQUE INDEX {key} ON {name} USING btree ({fields}){where}',
            'exclusion': False, 'options': [], 'ready': True, 'table_name': name,
            'unique': True, 'valid': True,
        }
    for section, entries in changes.items():
        for key, definition in entries.items():
            if key in expected[section] and expected[section][key] != definition:
                raise ValueError('conflicting online preparation object')
            expected[section][key] = deepcopy(definition)
    return reviewed_preparation_revisions_catalog_upgrade(reviewed_online_indexes_catalog_upgrade(expected))


def reviewed_online_proof_catalog_upgrade(baseline: dict) -> dict:
    """For already-applied online DDL, permit only the one proof column/check."""
    expected = deepcopy(baseline)
    table = 'public.retention_prepared_contracts'
    if table not in expected['tables']:
        raise ValueError('online preparation table is missing')
    for section, key, definition in (
        ('columns', f'{table}.validation_json',
         _column('jsonb', not_null=True, default="'{}'::jsonb")),
        ('constraints', f'{table}.retention_prepared_contracts_validation_check',
         _constraint("CHECK ((jsonb_typeof(validation_json) = 'object'::text))")),
    ):
        if key in expected[section] and expected[section][key] != definition:
            raise ValueError('conflicting online proof object')
        expected[section][key] = definition
    return expected



_ONLINE_LEGACY_WINDOW_INDEX = 'public.conversation_checkpoints_published_window_uq'
_ONLINE_PREPARED_WINDOW_INDEX = 'public.conversation_checkpoints_prepared_window_uq'
_ONLINE_LEGACY_WINDOW_OLD = "CREATE UNIQUE INDEX conversation_checkpoints_published_window_uq ON public.conversation_checkpoints USING btree (user_id, coverage_through_message_id) WHERE ((kind = 'window'::text) AND (publish_state = 'published'::text))"
_ONLINE_LEGACY_WINDOW_NEW = _ONLINE_LEGACY_WINDOW_OLD[:-1] + ' AND (preparation_id IS NULL))'
_ONLINE_PREPARED_WINDOW_NEW = "CREATE UNIQUE INDEX conversation_checkpoints_prepared_window_uq ON public.conversation_checkpoints USING btree (preparation_id, user_id, coverage_through_message_id) WHERE ((kind = 'window'::text) AND (publish_state = 'published'::text) AND (preparation_id IS NOT NULL))"


def reviewed_online_indexes_catalog_upgrade(baseline: dict) -> dict:
    """Partition only the existing window uniqueness by immutable preparation ID."""
    expected = deepcopy(baseline)
    previous = expected['indexes'].get(_ONLINE_LEGACY_WINDOW_INDEX)
    def definition(sql):
        return {'definition': sql, 'exclusion': False, 'options': [], 'ready': True,
                'table_name': 'public.conversation_checkpoints', 'unique': True, 'valid': True}
    if previous not in (definition(_ONLINE_LEGACY_WINDOW_OLD), definition(_ONLINE_LEGACY_WINDOW_NEW)):
        raise ValueError('unexpected legacy published window index')
    expected['indexes'][_ONLINE_LEGACY_WINDOW_INDEX] = definition(_ONLINE_LEGACY_WINDOW_NEW)
    prepared = definition(_ONLINE_PREPARED_WINDOW_NEW)
    if (_ONLINE_PREPARED_WINDOW_INDEX in expected['indexes']
            and expected['indexes'][_ONLINE_PREPARED_WINDOW_INDEX] != prepared):
        raise ValueError('conflicting prepared window index')
    expected['indexes'][_ONLINE_PREPARED_WINDOW_INDEX] = prepared
    return expected


_PREPARATION_REVISION_BODY = "DECLARE\n    old_row jsonb;\n    new_row jsonb;\n    old_owner uuid;\n    new_owner uuid;\n    owner_id uuid;\n    ignored_change boolean := false;\n    fence_value jsonb;\n    clock_entry record;\n    locked_through bigint;\n    mutating boolean;\nBEGIN\n    SELECT value INTO fence_value FROM public.app_config\n     WHERE key = 'context_retention_revision_fence' FOR SHARE;\n    IF NOT FOUND THEN\n        RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'retention_revision_fence_missing';\n    END IF;\n    IF NOT EXISTS (SELECT 1 FROM public.retention_preparations\n                   WHERE state IN ('building','prepared')) THEN\n        RETURN NULL;\n    END IF;\n    IF TG_OP <> 'INSERT' THEN old_row := to_jsonb(OLD); END IF;\n    IF TG_OP <> 'DELETE' THEN new_row := to_jsonb(NEW); END IF;\n    IF TG_TABLE_NAME = 'profiles' THEN\n        old_owner := (old_row->>'id')::uuid;\n        new_owner := (new_row->>'id')::uuid;\n    ELSE\n        old_owner := (old_row->>'user_id')::uuid;\n        new_owner := (new_row->>'user_id')::uuid;\n    END IF;\n    IF TG_OP = 'UPDATE' AND old_owner IS NOT DISTINCT FROM new_owner THEN\n        IF TG_TABLE_NAME = 'profiles'\n           AND old_row->'language' IS NOT DISTINCT FROM new_row->'language'\n           AND old_row->'timezone' IS NOT DISTINCT FROM new_row->'timezone' THEN\n            ignored_change := true;\n        ELSIF TG_TABLE_NAME = 'chat_contexts'\n           AND old_row->'memory_generation' IS NOT DISTINCT FROM new_row->'memory_generation' THEN\n            ignored_change := true;\n        ELSIF TG_TABLE_NAME = 'memory_pipeline_states'\n           AND old_row->'mode' IS NOT DISTINCT FROM new_row->'mode'\n           AND old_row->'bootstrap_status' IS NOT DISTINCT FROM new_row->'bootstrap_status'\n           AND old_row->'privacy_epoch' IS NOT DISTINCT FROM new_row->'privacy_epoch'\n           AND old_row->'repair_generation' IS NOT DISTINCT FROM new_row->'repair_generation'\n           AND COALESCE((new_row->>'source_through_turn_seq')::bigint,0) >= COALESCE((old_row->>'source_through_turn_seq')::bigint,0)\n           AND COALESCE((new_row->>'ingest_through_turn_seq')::bigint,0) >= COALESCE((old_row->>'ingest_through_turn_seq')::bigint,0)\n           AND COALESCE((new_row->>'consolidated_through_turn_seq')::bigint,0) >= COALESCE((old_row->>'consolidated_through_turn_seq')::bigint,0) THEN\n            ignored_change := true;\n        ELSIF TG_TABLE_NAME = 'privacy_subject_barriers'\n           AND old_row->'state' IS NOT DISTINCT FROM new_row->'state'\n           AND old_row->'epoch' IS NOT DISTINCT FROM new_row->'epoch' THEN\n            ignored_change := true;\n        END IF;\n    END IF;\n    FOR owner_id IN SELECT DISTINCT owner FROM unnest(ARRAY[old_owner,new_owner]) AS owner\n        WHERE owner IS NOT NULL ORDER BY owner\n    LOOP\n        -- A newly eligible/unregistered writer participates in the registry fence.\n        -- This never enrolls it or changes the approved source boundary.\n        PERFORM p.id FROM public.retention_preparations p\n         WHERE p.state IN ('building','prepared') AND NOT ignored_change\n           AND NOT EXISTS (SELECT 1 FROM public.retention_preparation_revisions r\n                WHERE r.preparation_id = p.id AND r.user_id = owner_id)\n           AND (TG_TABLE_NAME NOT IN ('conversation_checkpoints','retention_prepared_contracts')\n                OR (old_owner = owner_id AND (old_row->>'preparation_id')::uuid = p.id)\n                OR (new_owner = owner_id AND (new_row->>'preparation_id')::uuid = p.id))\n         ORDER BY p.id FOR KEY SHARE OF p;\n        FOR clock_entry IN\n            SELECT r.preparation_id,r.through_message_id\n              FROM public.retention_preparation_revisions r\n              JOIN public.retention_preparations p ON p.id = r.preparation_id\n             WHERE p.state IN ('building','prepared') AND r.user_id = owner_id\n               AND (TG_TABLE_NAME NOT IN ('conversation_checkpoints','retention_prepared_contracts')\n                    OR (old_owner = owner_id AND (old_row->>'preparation_id')::uuid = r.preparation_id)\n                    OR (new_owner = owner_id AND (new_row->>'preparation_id')::uuid = r.preparation_id))\n             ORDER BY r.preparation_id\n        LOOP\n            mutating := NOT ignored_change AND CASE WHEN TG_TABLE_NAME = 'messages' THEN\n                COALESCE(old_owner = owner_id AND (old_row->>'id')::bigint <= clock_entry.through_message_id,false)\n                OR COALESCE(new_owner = owner_id AND (new_row->>'id')::bigint <= clock_entry.through_message_id,false)\n                ELSE true END;\n            BEGIN\n                IF mutating THEN\n                    SELECT r.through_message_id INTO locked_through\n                      FROM public.retention_preparation_revisions r\n                     WHERE r.preparation_id = clock_entry.preparation_id AND r.user_id = owner_id\n                     FOR UPDATE NOWAIT;\n                ELSE\n                    SELECT r.through_message_id INTO locked_through\n                      FROM public.retention_preparation_revisions r\n                     WHERE r.preparation_id = clock_entry.preparation_id AND r.user_id = owner_id\n                     FOR SHARE NOWAIT;\n                END IF;\n                IF locked_through IS NULL THEN CONTINUE; END IF;\n                mutating := NOT ignored_change AND CASE WHEN TG_TABLE_NAME = 'messages' THEN\n                    COALESCE(old_owner = owner_id AND (old_row->>'id')::bigint <= locked_through,false)\n                    OR COALESCE(new_owner = owner_id AND (new_row->>'id')::bigint <= locked_through,false)\n                    ELSE true END;\n                IF mutating THEN\n                    -- A shared read can see a newly extended boundary. Upgrade\n                    -- only NOWAIT; concurrent upgrades fall back to events.\n                    PERFORM 1 FROM public.retention_preparation_revisions r\n                     WHERE r.preparation_id = clock_entry.preparation_id AND r.user_id = owner_id\n                     FOR UPDATE NOWAIT;\n                    UPDATE public.retention_preparation_revisions r SET revision = r.revision + 1\n                     WHERE r.preparation_id = clock_entry.preparation_id AND r.user_id = owner_id;\n                END IF;\n            EXCEPTION WHEN lock_not_available THEN\n                -- Never wait for another writer's clock while owning source rows.\n                -- Registry sharing orders both semantic events and ignored tails\n                -- against promotion; the clock itself is not an event FK target.\n                PERFORM p.id FROM public.retention_preparations p\n                 WHERE p.id = clock_entry.preparation_id AND p.state IN ('building','prepared')\n                 FOR KEY SHARE OF p;\n                IF NOT FOUND THEN CONTINUE; END IF;\n                SELECT r.through_message_id INTO locked_through\n                  FROM public.retention_preparation_revisions r\n                 WHERE r.preparation_id = clock_entry.preparation_id AND r.user_id = owner_id;\n                mutating := locked_through IS NOT NULL AND NOT ignored_change\n                    AND CASE WHEN TG_TABLE_NAME = 'messages' THEN\n                        COALESCE(old_owner = owner_id AND (old_row->>'id')::bigint <= locked_through,false)\n                        OR COALESCE(new_owner = owner_id AND (new_row->>'id')::bigint <= locked_through,false)\n                        ELSE true END;\n                IF mutating THEN\n                    INSERT INTO public.retention_preparation_revision_events(preparation_id,user_id)\n                    VALUES (clock_entry.preparation_id,owner_id);\n                END IF;\n            END;\n        END LOOP;\n    END LOOP;\n    RETURN NULL;\nEND;\n"
_PREPARATION_REVISION_TABLES = ('profiles', 'chat_contexts', 'privacy_subject_barriers', 'memory_pipeline_states', 'messages', 'mem0_memory_registry', 'mem0_memory_sources', 'conversation_checkpoints', 'retention_prepared_contracts', 'mem0_ingest_candidates')


def reviewed_preparation_revisions_catalog_upgrade(baseline: dict) -> dict:
    expected = deepcopy(baseline)
    table = 'public.retention_preparation_revisions'
    changes = {section: {} for section in baseline}
    changes['tables'][table] = deepcopy(baseline['tables']['public.chat_active_turns'])
    for key, value in baseline['relation_grants'].items():
        if key.startswith('public.chat_active_turns:'):
            changes['relation_grants'][table + key[len('public.chat_active_turns'):]] = deepcopy(value)
    changes['columns'] = {f'{table}.{key}': value for key, value in {
        'preparation_id': _column('uuid', not_null=True),
        'user_id': _column('uuid', not_null=True),
        'through_message_id': _column('bigint', not_null=True),
        'revision': _column('bigint', not_null=True, default='0'),
    }.items()}
    changes['constraints'] = {
        f'{table}.retention_preparation_revisions_pkey': _constraint('PRIMARY KEY (preparation_id, user_id)'),
        f'{table}.retention_preparation_revisions_through_check': _constraint('CHECK ((through_message_id >= 0))'),
        f'{table}.retention_preparation_revisions_revision_check': _constraint('CHECK ((revision >= 0))'),
    }
    for column, target, cascade in (('preparation_id', 'public.retention_preparations', False),
                                    ('user_id', 'public.profiles', True)):
        definition = _constraint(f'FOREIGN KEY ({column}) REFERENCES {target}(id)' + (' ON DELETE CASCADE' if cascade else ''))
        definition['enforcement_triggers'] = sorted([f'{table}:17:O', f'{table}:5:O', f'{target}:17:O', f'{target}:9:O'])
        changes['constraints'][f'{table}.retention_preparation_revisions_{column}_fkey'] = definition
    for name, fields, unique in (('retention_preparation_revisions_pkey', 'preparation_id, user_id', True),
                                 ('retention_preparation_revisions_user_idx', 'user_id', False)):
        changes['indexes'][f'public.{name}'] = {
            'definition': f"CREATE {'UNIQUE ' if unique else ''}INDEX {name} ON {table} USING btree ({fields})",
            'exclusion': False, 'options': [], 'ready': True, 'table_name': table,
            'unique': unique, 'valid': True}
    function = 'public.bump_retention_preparation_revision()'
    changes['functions'][function] = {
        'definition': "CREATE OR REPLACE FUNCTION public.bump_retention_preparation_revision()\n RETURNS trigger\n LANGUAGE plpgsql\n SECURITY DEFINER\n SET search_path TO ''\nAS $function$\n" + _PREPARATION_REVISION_BODY + '$function$\n',
        'owner': baseline['tables']['public.chat_active_turns']['owner']}
    for name in _PREPARATION_REVISION_TABLES:
        trigger = f'{name}_retention_preparation_revision'
        changes['triggers'][f'public.{name}.{trigger}'] = {
            'definition': f'CREATE TRIGGER {trigger} AFTER INSERT OR DELETE OR UPDATE ON public.{name} FOR EACH ROW EXECUTE FUNCTION public.bump_retention_preparation_revision()',
            'enabled': 'O'}
    for key in expected['function_grants']:
        if key.startswith(function + ':'):
            raise ValueError('unexpected preparation revision function grant')
    for section, entries in changes.items():
        for key, definition in entries.items():
            if key in expected[section] and expected[section][key] != definition:
                raise ValueError('conflicting preparation revision object')
            expected[section][key] = deepcopy(definition)
    return reviewed_preparation_revision_events_catalog_upgrade(expected)



def reviewed_preparation_revision_events_catalog_upgrade(baseline: dict) -> dict:
    expected = deepcopy(baseline)
    table = 'public.retention_preparation_revision_events'
    sequence = table + '_id_seq'
    changes = {section: {} for section in baseline}
    changes['tables'][table] = deepcopy(baseline['tables']['public.chat_active_turns'])
    changes['relation_grants'][table + ':service_role:SELECT'] = {'grantable': False}
    fields = {'id': _column('bigint', not_null=True, default=f"nextval('{sequence}'::regclass)"),
              'preparation_id': _column('uuid', not_null=True), 'user_id': _column('uuid', not_null=True)}
    changes['columns'] = {f'{table}.{key}': value for key, value in fields.items()}
    for field, kind in (('last_value', 'bigint'), ('log_cnt', 'bigint'), ('is_called', 'boolean')):
        changes['columns'][sequence + '.' + field] = _column(kind, not_null=True)
    changes['sequences'][sequence] = deepcopy(baseline['sequences']['public.messages_id_seq'])
    changes['constraints'][table + '.retention_preparation_revision_events_pkey'] = _constraint('PRIMARY KEY (id)')
    for field, target, cascade in (('preparation_id', 'public.retention_preparations', False),
                                    ('user_id', 'public.profiles', True)):
        value = _constraint(f'FOREIGN KEY ({field}) REFERENCES {target}(id)' + (' ON DELETE CASCADE' if cascade else ''))
        value['enforcement_triggers'] = sorted([f'{table}:17:O', f'{table}:5:O', f'{target}:17:O', f'{target}:9:O'])
        changes['constraints'][f'{table}.retention_preparation_revision_events_{field}_fkey'] = value
    for name, fields, unique in (('retention_preparation_revision_events_pkey', 'id', True),
                                 ('retention_preparation_revision_events_subject_idx', 'preparation_id, user_id', False)):
        changes['indexes'][f'public.{name}'] = {
            'definition': f"CREATE {'UNIQUE ' if unique else ''}INDEX {name} ON {table} USING btree ({fields})",
            'exclusion': False, 'options': [], 'ready': True, 'table_name': table,
            'unique': unique, 'valid': True}
    for section, entries in changes.items():
        for key, value in entries.items():
            if key in expected[section] and expected[section][key] != value:
                raise ValueError('conflicting preparation revision event object')
            expected[section][key] = deepcopy(value)
    return expected


def require_reviewed_dev(dsn: str, *, env: str, expected_project_ref: str) -> None:
    """Refuse prod, unknown hosts/refs, and URL connection overrides before connecting."""
    from db.envfile import DEV_PROJECT_REFS, project_ref

    target = urlparse(dsn)
    host = target.hostname or ''
    if (env != 'dev' or expected_project_ref not in DEV_PROJECT_REFS
            or project_ref(dsn) != expected_project_ref
            or target.scheme not in {'postgres', 'postgresql'}
            or target.query or target.fragment or target.path != '/postgres'
            or target.port not in {None, 5432, 6543}
            or not (host.endswith('.pooler.supabase.com')
                    or host == f'db.{expected_project_ref}.supabase.co')):
        raise ValueError('refresh requires the explicitly confirmed development Supabase target')


async def refresh_from_dev(
    dsn: str, *, env: str, expected_project_ref: str, reviewed_sql: Path,
    expected_sha256: str, baseline_schema: Path, strict: bool = False,
) -> list[str]:
    """Read dev only; regenerate this release without adopting unrelated live drift."""
    require_reviewed_dev(dsn, env=env, expected_project_ref=expected_project_ref)
    guard_context_release = reviewed_sql.resolve() == REVIEWED_GUARD_CONTEXT_SQL.resolve()
    if not guard_context_release and reviewed_sql.resolve() != REVIEWED_DEV_SQL.resolve():
        raise ValueError('only this release reviewed SQL is supported')
    sql_bytes = reviewed_sql.read_bytes()
    if (not re.fullmatch(r'[0-9a-f]{64}', expected_sha256)
            or hashlib.sha256(sql_bytes).hexdigest() != expected_sha256):
        raise ValueError('reviewed SQL checksum mismatch')
    original_contract = CONTRACT.read_bytes()
    original_schema = SCHEMA.read_bytes()
    original_baseline = baseline_schema.read_bytes()
    contract = json.loads(original_contract)
    if hashlib.sha256(original_baseline).hexdigest() != contract['schema_sha256']:
        raise ValueError('baseline schema does not match the existing generated contract')
    source_upgrade = reviewed_guard_context_schema_upgrade if guard_context_release else reviewed_schema_upgrade
    catalog_upgrade = reviewed_guard_context_catalog_upgrade if guard_context_release else reviewed_catalog_upgrade
    upgraded_source = source_upgrade(original_baseline.decode())
    if upgraded_source.encode() != original_schema:
        raise ValueError('schema.sql contains changes outside the reviewed release')
    expected = catalog_upgrade(contract['catalog'])
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=10, command_timeout=15)
    try:
        # read_catalog owns a repeatable-read, readonly transaction and only reads
        # PostgreSQL metadata. Never execute reviewed SQL or schema.sql here.
        actual = await read_catalog(conn)
    finally:
        await conn.close()
    problems = compare_catalog(expected, actual, strict=strict)
    if problems:
        return problems
    generated = json.dumps(
        {'schema_sha256': hashlib.sha256(original_schema).hexdigest(), 'catalog': expected},
        ensure_ascii=False, indent=2, sort_keys=True,
    ) + '\n'
    tmp = CONTRACT.with_name(f'.{CONTRACT.name}.dev-refresh-{uuid.uuid4().hex}')
    try:
        with tmp.open('x', encoding='utf-8') as stream:
            stream.write(generated)
        if (CONTRACT.read_bytes() != original_contract or SCHEMA.read_bytes() != original_schema
                or baseline_schema.read_bytes() != original_baseline
                or reviewed_sql.read_bytes() != sql_bytes):
            raise ValueError('reviewed inputs changed during catalog inspection')
        os.replace(tmp, CONTRACT)
    finally:
        tmp.unlink(missing_ok=True)
    return []


GUARD_BASELINE_SCHEMA_SHA256 = 'e5929c8dc95a01842822ff94be432d8df791244ffdbf91820310d3f94a403e7a'
GUARD_BASELINE_CONTRACT_SHA256 = '769d36eb70564f841364b23dbe11ccd63a2b8b075beda1dd26ba856aa206d014'
_GUARD_SQL_DIGESTS = {
    'crisis_guardrails.sql': 'c587c3524e186a8a031c69d5e6769851819ed2a13c4990958f5ebe5466c3da13',
    'guard_context_v2.sql': 'e4c58f65889444430868e676e6d37702733bd30ff0c407c95686937e58145bb6',
    'guard_context_online.sql': '55a9a01050eb0a2329ad1428840cf795a49fc424ea55facf887ec13c2a2a1ac9',
    'guard_context_online_proof.sql': '2c203ba8e192733a1efc60cb75ce269ab8bdded6cdb01ef2d77940048309f54a',
    'guard_context_online_indexes.sql': '3ea78345283bc99647f8aceee60329e69e60e92a6d6712f853391d87c04a6276',
    'guard_context_online_revisions.sql': 'fb1a72780ad5802bd9afde7c2ee5fabb8a240d9f452d1f1eaaac638a0cd5d8e3',
    'guard_context_online_revision_fence.sql': '42b3ac96c08e668068800ae18b5eb048bb282b4431c18aa01fb15252a78fab13',
    'guard_context_online_revision_events.sql': '8cbfd822e48c99fa44de18b60fb935a9344f67aad371a65764ffcea9307ff227',
}


def derive_guard_context_candidate(*, baseline_schema: Path, baseline_contract: Path,
                                   crisis_sha256: str, pressure_sha256: str, online_sha256: str) -> list[str]:
    """Derive this release candidate from reviewed main64, without connecting to a DB.

    This is a manifest derivation, not a database attestation. Preserve all main
    catalog objects and require live catalog preflight before applying the SQL.
    The existing live verify/generate and subscription workflows remain separate.
    """
    baseline = baseline_schema.read_bytes()
    original_baseline_contract = baseline_contract.read_bytes()
    if (hashlib.sha256(baseline).hexdigest() != GUARD_BASELINE_SCHEMA_SHA256
            or hashlib.sha256(original_baseline_contract).hexdigest() != GUARD_BASELINE_CONTRACT_SHA256):
        raise ValueError('guard candidate requires the reviewed main64 baseline')
    artifact = json.loads(original_baseline_contract)
    if artifact['schema_sha256'] != hashlib.sha256(baseline).hexdigest():
        raise ValueError('reviewed main baseline source and contract disagree')
    reviewed = ((REVIEWED_DEV_SQL, crisis_sha256), (REVIEWED_GUARD_CONTEXT_SQL, pressure_sha256),
                (REVIEWED_ONLINE_SQL, online_sha256))
    sql_inputs = []
    for path, expected in reviewed:
        data = path.read_bytes()
        if (expected != _GUARD_SQL_DIGESTS[path.name]
                or hashlib.sha256(data).hexdigest() != expected):
            raise ValueError('guard candidate reviewed SQL checksum mismatch')
        sql_inputs.append((path, data))
    source = SCHEMA.read_bytes()
    prior_source = reviewed_guard_context_schema_upgrade(reviewed_schema_upgrade(baseline.decode()))
    upgraded = reviewed_online_schema_upgrade(prior_source)
    if upgraded.encode() != source:
        raise ValueError('schema contains changes outside the reviewed main guard release')
    prior_catalog = reviewed_guard_context_catalog_upgrade(reviewed_catalog_upgrade(artifact['catalog']))
    catalog = reviewed_online_catalog_upgrade(prior_catalog)
    prior_candidate = {'schema_sha256': hashlib.sha256(prior_source.encode()).hexdigest(), 'catalog': prior_catalog}
    candidate = {'schema_sha256': hashlib.sha256(source).hexdigest(), 'catalog': catalog}
    original_current = CONTRACT.read_bytes()
    if (original_current != original_baseline_contract
            and json.loads(original_current) not in (prior_candidate, candidate)):
        raise ValueError('current contract is neither reviewed main nor this derived candidate')
    generated = json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    temporary = CONTRACT.with_name('.guard-candidate-' + uuid.uuid4().hex)
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            stream.write(generated)
        if (baseline_schema.read_bytes() != baseline or SCHEMA.read_bytes() != source
                or baseline_contract.read_bytes() != original_baseline_contract
                or CONTRACT.read_bytes() != original_current
                or any(path.read_bytes() != data for path, data in sql_inputs)):
            raise ValueError('guard candidate reviewed inputs changed during derivation')
        os.replace(temporary, CONTRACT)
    finally:
        temporary.unlink(missing_ok=True)
    return []


def schema_digest() -> str:
    return hashlib.sha256(SCHEMA.read_bytes()).hexdigest()


def load_contract() -> dict:
    contract = json.loads(CONTRACT.read_text())
    if contract['schema_sha256'] != schema_digest():
        raise ValueError('schema.sql changed: regenerate the catalog contract in a fresh local DB')
    return contract['catalog']


async def verify(dsn: str, *, strict: bool = False) -> list[str]:
    expected = load_contract()
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=10, command_timeout=15)
    try:
        actual = await read_catalog(conn)
    finally:
        await conn.close()
    return compare_catalog(expected, actual, strict=strict)


def require_scratch(dsn: str) -> None:
    target = urlparse(dsn)
    # Query parameters can override the URL host/database in asyncpg. Do not accept
    # alternate connection targets hidden behind an apparently local URL.
    if (target.scheme not in {'postgresql', 'postgres'}
            or target.hostname not in {'localhost', '127.0.0.1', '::1'}
            or not re.fullmatch(r'/moly_schema_[A-Za-z0-9_]+', target.path)
            or target.query or target.fragment):
        raise ValueError('generation requires a local disposable database named moly_schema_*')


async def generate(dsn: str, *, check: bool = False) -> list[str]:
    """Create the schema in a disposable DB, then derive/check the committed contract."""
    require_scratch(dsn)
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=10, command_timeout=60)
    try:
        await conn.execute(SCHEMA.read_text())
        catalog = await read_catalog(conn)
    finally:
        await conn.close()
    if check:
        return compare_catalog(load_contract(), catalog, strict=True)
    CONTRACT.write_text(json.dumps(
        {'schema_sha256': schema_digest(), 'catalog': catalog},
        ensure_ascii=False, indent=2, sort_keys=True,
    ) + '\n')
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strict', action='store_true', help='also reject additive objects')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--generate', action='store_true', help='write contract from fresh local DB')
    modes.add_argument('--check-generated', action='store_true', help='rebuild schema and check contract')
    modes.add_argument('--refresh-subscription-from-dev', action='store_true', help='preserve main baseline and derive two reviewed dev columns')
    modes.add_argument('--refresh-from-dev', action='store_true', help='read-only dev catalog check for a reviewed guard SQL delta')
    modes.add_argument('--derive-guard-context-candidate', action='store_true', help='derive reviewed main64 guard catalog candidate without any DB connection; live preflight remains required')
    parser.add_argument('--env', default='dev')
    parser.add_argument('--expected-project-ref')
    parser.add_argument('--reviewed-sql', type=Path)
    parser.add_argument('--expected-sha256')
    parser.add_argument('--baseline-schema', type=Path)
    parser.add_argument('--baseline-contract', type=Path)
    parser.add_argument('--crisis-sha256')
    parser.add_argument('--pressure-sha256')
    parser.add_argument('--online-sha256')
    args = parser.parse_args()
    raw = os.environ.get('SUPABASE_DB_CONNECTION_STRING', '')
    if not raw and not args.refresh_subscription_from_dev and not args.derive_guard_context_candidate:
        parser.error('SUPABASE_DB_CONNECTION_STRING is required')
    dsn = re.sub(r'^postgresql\+asyncpg://', 'postgresql://', raw)
    try:
        if args.derive_guard_context_candidate:
            if not all((args.baseline_schema, args.baseline_contract, args.crisis_sha256, args.pressure_sha256, args.online_sha256)):
                parser.error('reviewed main baseline and all three guard SQL checksums required')
            problems = derive_guard_context_candidate(baseline_schema=args.baseline_schema,
                baseline_contract=args.baseline_contract, crisis_sha256=args.crisis_sha256,
                pressure_sha256=args.pressure_sha256, online_sha256=args.online_sha256)
        elif args.refresh_from_dev:
            if not all((args.expected_project_ref, args.reviewed_sql, args.expected_sha256, args.baseline_schema)):
                parser.error('reviewed guard release arguments required')
            problems = asyncio.run(refresh_from_dev(dsn, env=args.env,
                expected_project_ref=args.expected_project_ref, reviewed_sql=args.reviewed_sql,
                expected_sha256=args.expected_sha256, baseline_schema=args.baseline_schema, strict=args.strict))
        elif args.refresh_subscription_from_dev:
            if not all((args.expected_project_ref, args.reviewed_sql, args.expected_sha256, args.baseline_schema)):
                parser.error('reviewed subscription release arguments required')
            problems = asyncio.run(refresh_subscription_from_dev(
                env=args.env, expected_project_ref=args.expected_project_ref,
                reviewed_sql=args.reviewed_sql, expected_sha256=args.expected_sha256,
                baseline_schema=args.baseline_schema))
        elif args.generate or args.check_generated:
            problems = asyncio.run(generate(dsn, check=args.check_generated))
        else:
            problems = asyncio.run(verify(dsn, strict=args.strict))
    except Exception as exc:
        # Connection errors may contain credentials; only catalog differences are printable.
        print(f'Schema check failed ({type(exc).__name__}); check target and contract generation')
        return 1
    for problem in problems:
        print(problem)
    if not problems:
        print('Schema contract candidate OK (reviewed derivation only; database not checked)'
              if args.derive_guard_context_candidate else 'Schema contract OK')
    return int(bool(problems))


if __name__ == '__main__':
    raise SystemExit(main())
