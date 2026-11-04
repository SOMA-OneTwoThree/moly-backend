-- 기존 revision 구조/10개 trigger가 있는 개발 DB의 함수 보완만 수행한다.
-- db.apply --statement-timeout-ms 750 사용. 먼저 이전 trigger를 끝낸 미commit writer까지 배제한다.
-- 기존 자료/테이블/trigger를 재생성하지 않는다. seed와 함수 교체는 같은 TX다.
LOCK TABLE public.profiles, public.chat_contexts, public.privacy_subject_barriers,
    public.memory_pipeline_states, public.messages, public.mem0_memory_registry,
    public.mem0_memory_sources, public.conversation_checkpoints,
    public.retention_prepared_contracts, public.mem0_ingest_candidates
    IN SHARE ROW EXCLUSIVE MODE NOWAIT;

-- 기술 metadata 행이며 사용자 기능 flag가 아니다. 기존 번호는 초기화하지 않는다.
INSERT INTO public.app_config (key,value)
VALUES ('context_retention_revision_fence','0'::jsonb) ON CONFLICT (key) DO NOTHING;

CREATE OR REPLACE FUNCTION public.bump_retention_preparation_revision() RETURNS trigger
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
        UPDATE public.retention_preparation_revisions r
           SET revision = r.revision + 1
          FROM public.retention_preparations p
         WHERE p.id = r.preparation_id AND p.state IN ('building','prepared')
           AND r.user_id = owner_id AND NOT ignored_change
           AND (TG_TABLE_NAME <> 'messages'
                OR (old_owner = owner_id AND (old_row->>'id')::bigint <= r.through_message_id)
                OR (new_owner = owner_id AND (new_row->>'id')::bigint <= r.through_message_id))
           AND (TG_TABLE_NAME NOT IN ('conversation_checkpoints','retention_prepared_contracts')
                OR (old_owner = owner_id AND (old_row->>'preparation_id')::uuid = r.preparation_id)
                OR (new_owner = owner_id AND (new_row->>'preparation_id')::uuid = r.preparation_id));
        -- Ignored metadata/forward progress and >T raw keep a shared fence.
        -- Mutating rows go directly to UPDATE, avoiding concurrent SHARE upgrades.
        PERFORM r.preparation_id FROM public.retention_preparation_revisions r
          JOIN public.retention_preparations p ON p.id = r.preparation_id
         WHERE p.state IN ('building','prepared') AND r.user_id = owner_id
           AND (ignored_change OR (TG_TABLE_NAME = 'messages'
                AND NOT (COALESCE(old_owner = owner_id AND (old_row->>'id')::bigint <= r.through_message_id,false)
                     OR COALESCE(new_owner = owner_id AND (new_row->>'id')::bigint <= r.through_message_id,false))))
         ORDER BY r.preparation_id FOR SHARE OF r;
    END LOOP;
    RETURN NULL;
END;
$function$;
REVOKE ALL ON FUNCTION public.bump_retention_preparation_revision() FROM PUBLIC,anon,authenticated,service_role;
