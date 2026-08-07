-- Dev rollout migration: conversation-centred recall, exact suppression, diary prologue,
-- active-turn CAS, typed diary references and bounded retention metadata.
-- Additive where possible. The legacy diary (user_id, diary_date) unique constraint is replaced
-- because welcome and a daily diary must coexist on the same display date.
BEGIN;

-- ── [전환 사본] 선점 잠금 — 교착 방지 ─────────────────────────────────────────
-- 이 파일은 여러 표에 차례로 DDL을 건다. 그 사이 앱 요청이 다른 순서로 같은 표를
-- 잡으면 교착이 난다(2026-08-07 실제로 발생: 이쪽이 profiles를 쥐고 idempotency_keys를
-- 기다리는 동안, 대화 요청이 idempotency_keys를 쥐고 profiles를 기다렸다).
-- 필요한 표를 처음에 한꺼번에 잡으면 순서가 엇갈릴 수 없다.
-- 10초 안에 못 잡으면 깨끗이 포기하고 되돌린다 — 앱을 오래 세우지 않는다.
SET LOCAL lock_timeout = '10s';
LOCK TABLE public.chat_contexts, public.diaries, public.idempotency_keys,
           public.messages, public.profiles, public.routine_completions, public.routines
  IN ACCESS EXCLUSIVE MODE;
SET LOCAL lock_timeout = '0';
-- ── 선점 잠금 끝 ──────────────────────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS public.schema_migrations (
  migration_name text PRIMARY KEY,
  checksum_sha256 text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now(),
  applied_by text NOT NULL DEFAULT current_user
);
ALTER TABLE public.schema_migrations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.schema_migrations FROM anon, authenticated;

-- Relationship origin is the first committed conversation, not auth/profile creation.
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS relationship_started_at timestamptz,
  ADD COLUMN IF NOT EXISTS relationship_started_timezone text,
  ADD COLUMN IF NOT EXISTS relationship_display_date date,
  ADD COLUMN IF NOT EXISTS next_diary_due_at timestamptz;

WITH first_turn AS (
  SELECT DISTINCT ON (m.user_id) m.user_id, m.created_at
  FROM public.messages m
  WHERE m.sender='user'
  ORDER BY m.user_id, m.id
)
UPDATE public.profiles p
SET relationship_started_at = f.created_at,
    relationship_started_timezone = p.timezone,
    relationship_display_date = (f.created_at AT TIME ZONE p.timezone)::date
FROM first_turn f
WHERE p.id=f.user_id AND p.relationship_started_at IS NULL;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='profiles_relationship_origin_ck'
      AND conrelid='public.profiles'::regclass
  ) THEN
    ALTER TABLE public.profiles ADD CONSTRAINT profiles_relationship_origin_ck CHECK (
      num_nonnulls(relationship_started_at, relationship_started_timezone, relationship_display_date)
      IN (0, 3)
    );
  END IF;
END $$;

-- Tenant candidate keys and committed-turn coordinates.
ALTER TABLE public.messages
  ADD COLUMN IF NOT EXISTS turn_seq bigint,
  ADD COLUMN IF NOT EXISTS turn_position smallint;
CREATE UNIQUE INDEX IF NOT EXISTS messages_user_id_id_uq ON public.messages(user_id,id);
CREATE UNIQUE INDEX IF NOT EXISTS messages_user_id_id_sender_uq ON public.messages(user_id,id,sender);
CREATE UNIQUE INDEX IF NOT EXISTS messages_user_turn_position_uq
  ON public.messages(user_id,turn_seq,turn_position) WHERE turn_seq IS NOT NULL;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname='messages_turn_position_ck'
      AND conrelid='public.messages'::regclass
  ) THEN
    ALTER TABLE public.messages ADD CONSTRAINT messages_turn_position_ck
      CHECK ((turn_seq IS NULL AND turn_position IS NULL)
             OR (turn_seq > 0 AND turn_position BETWEEN 0 AND 2));
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS routines_user_id_id_uq ON public.routines(user_id,id);
DELETE FROM public.routine_completions c
USING public.routines r
WHERE c.routine_id=r.id AND c.user_id<>r.user_id;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname='routine_completions_user_routine_fk'
      AND conrelid='public.routine_completions'::regclass
  ) THEN
    ALTER TABLE public.routine_completions
      ADD CONSTRAINT routine_completions_user_routine_fk
      FOREIGN KEY (user_id,routine_id) REFERENCES public.routines(user_id,id) ON DELETE CASCADE;
  END IF;
END $$;

-- Diary: product kind and display/activity coordinates replace the single daily slot.
ALTER TABLE public.diaries
  ADD COLUMN IF NOT EXISTS kind text,
  ADD COLUMN IF NOT EXISTS activity_date date,
  ADD COLUMN IF NOT EXISTS display_date date,
  ADD COLUMN IF NOT EXISTS title text,
  ADD COLUMN IF NOT EXISTS author text NOT NULL DEFAULT 'capi',
  ADD COLUMN IF NOT EXISTS occurred_at timestamptz,
  ADD COLUMN IF NOT EXISTS occurred_timezone text,
  ADD COLUMN IF NOT EXISTS occurred_timezone_provenance text,
  ADD COLUMN IF NOT EXISTS primary_subject text,
  ADD COLUMN IF NOT EXISTS about_tags text[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS content_version integer NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS record_status text NOT NULL DEFAULT 'published',
  ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

UPDATE public.diaries SET
  kind = CASE source WHEN 'welcome' THEN 'welcome' WHEN 'llm' THEN 'shared_day'
                     WHEN 'preset' THEN 'capi_day' ELSE NULL END,
  activity_date = CASE WHEN source IN ('llm','preset') THEN diary_date ELSE NULL END,
  display_date = diary_date,
  author = 'capi',
  occurred_timezone_provenance = COALESCE(occurred_timezone_provenance, 'legacy_unknown'),
  primary_subject = CASE WHEN source IN ('welcome','llm') THEN 'user' ELSE 'capi' END,
  about_tags = CASE WHEN source IN ('welcome','llm') THEN ARRAY['user']::text[]
                    WHEN source='preset' THEN ARRAY['capi']::text[] ELSE '{}'::text[] END,
  record_status = CASE WHEN source='none' THEN 'processed' ELSE 'published' END
WHERE display_date IS NULL OR kind IS NULL;


-- ── [전환 사본] 구 코드 호환 트리거 — db/cutover/diaries_legacy_compat_trigger.sql ──
CREATE OR REPLACE FUNCTION public.diaries_legacy_compat()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.display_date IS NULL THEN
    NEW.display_date := NEW.diary_date;
  END IF;

  IF NEW.kind IS NULL THEN
    NEW.kind := CASE NEW.source
                  WHEN 'welcome' THEN 'welcome'
                  WHEN 'llm'     THEN 'shared_day'
                  WHEN 'preset'  THEN 'capi_day'
                  ELSE NULL
                END;
  END IF;

  IF NEW.activity_date IS NULL AND NEW.kind IN ('shared_day', 'capi_day') THEN
    NEW.activity_date := NEW.diary_date;
  END IF;

  -- source='none'은 사용자에게 안 보이는 처리 표시다. record_status 기본값이 'published'라
  -- 그대로 두면 kind가 비어 있어 `diaries_kind_ck`에 걸린다.
  IF NEW.source = 'none' AND NEW.record_status = 'published' THEN
    NEW.record_status := 'processed';
  END IF;

  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS diaries_legacy_compat_tg ON public.diaries;
CREATE TRIGGER diaries_legacy_compat_tg
  BEFORE INSERT ON public.diaries
  FOR EACH ROW
  EXECUTE FUNCTION public.diaries_legacy_compat();
-- ── 트리거 끝 ──

ALTER TABLE public.diaries ALTER COLUMN display_date SET NOT NULL;
-- [전환 사본] 118행 제거 — 3단계로 미룸: ALTER TABLE public.diaries DROP CONSTRAINT IF EXISTS diaries_user_date_uq;
CREATE UNIQUE INDEX IF NOT EXISTS diaries_user_id_id_uq ON public.diaries(user_id,id);
CREATE UNIQUE INDEX IF NOT EXISTS diaries_one_welcome_uq
  ON public.diaries(user_id) WHERE kind='welcome' AND deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS diaries_one_daily_uq
  ON public.diaries(user_id,activity_date)
  WHERE kind IN ('shared_day','capi_day') AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS diaries_user_display_cursor_idx
  ON public.diaries(user_id,display_date DESC,id DESC)
  WHERE record_status='published' AND deleted_at IS NULL;
DROP INDEX IF EXISTS public.diaries_content_trgm_idx;

CREATE TABLE IF NOT EXISTS public.diary_generation_results (
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  target_date date NOT NULL,
  status text NOT NULL CHECK (status IN ('no_entry')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id,target_date)
);
INSERT INTO public.diary_generation_results(user_id,target_date,status,created_at)
SELECT user_id,diary_date,'no_entry',COALESCE(created_at,now())
FROM public.diaries WHERE source='none'
ON CONFLICT (user_id,target_date) DO NOTHING;
-- [전환 사본] 141행 제거 — 3단계로 미룸: DELETE FROM public.diaries WHERE source='none';

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname='diaries_kind_ck'
      AND conrelid='public.diaries'::regclass
  ) THEN
    ALTER TABLE public.diaries ADD CONSTRAINT diaries_kind_ck CHECK (
      (record_status='processed' AND kind IS NULL)
      OR (record_status IN ('draft','published') AND kind IN ('welcome','shared_day','capi_day'))
      OR (record_status='deleted')
    );
    ALTER TABLE public.diaries ADD CONSTRAINT diaries_kind_activity_ck CHECK (
      (kind='welcome' AND activity_date IS NULL)
      OR (kind IN ('shared_day','capi_day') AND activity_date IS NOT NULL)
      OR kind IS NULL
    );
    ALTER TABLE public.diaries ADD CONSTRAINT diaries_author_ck CHECK (author='capi');
  END IF;
END $$;

-- Chat revision, one active inference lease per user and exact idempotency semantics.
ALTER TABLE public.chat_contexts
  ADD COLUMN IF NOT EXISTS context_revision bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS last_committed_turn_seq bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS prompt_cache_generation bigint NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS public.chat_active_turns (
  user_id uuid PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  turn_seq bigint NOT NULL CHECK (turn_seq > 0),
  idempotency_key text NOT NULL,
  request_hash text NOT NULL,
  base_context_revision bigint NOT NULL,
  lease_token uuid NOT NULL,
  lease_until timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS chat_active_turns_key_uq
  ON public.chat_active_turns(user_id,idempotency_key);

ALTER TABLE public.idempotency_keys
  ALTER COLUMN response DROP NOT NULL,
  ADD COLUMN IF NOT EXISTS request_hash text,
  ADD COLUMN IF NOT EXISTS response_schema_version bigint NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS reply_message_id bigint,
  ADD COLUMN IF NOT EXISTS terminal_status text NOT NULL DEFAULT 'succeeded',
  ADD COLUMN IF NOT EXISTS response_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS dedupe_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS redacted_at timestamptz;

UPDATE public.idempotency_keys
SET response_expires_at=COALESCE(response_expires_at,created_at+interval '24 hours'),
    dedupe_expires_at=COALESCE(dedupe_expires_at,created_at+interval '30 days'),
    reply_message_id=CASE
      WHEN response #>> '{reply,message_id}' ~ '^[0-9]+$'
      THEN (response #>> '{reply,message_id}')::bigint ELSE reply_message_id END
WHERE response_expires_at IS NULL OR dedupe_expires_at IS NULL OR reply_message_id IS NULL;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname='idempotency_reply_message_fk'
      AND conrelid='public.idempotency_keys'::regclass
  ) THEN
    ALTER TABLE public.idempotency_keys ADD CONSTRAINT idempotency_reply_message_fk
      FOREIGN KEY (user_id,reply_message_id) REFERENCES public.messages(user_id,id)
      ON DELETE CASCADE;
    ALTER TABLE public.idempotency_keys ADD CONSTRAINT idempotency_terminal_status_ck
      CHECK (terminal_status IN ('succeeded','expired','redacted'));
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS idempotency_reply_idx
  ON public.idempotency_keys(user_id,reply_message_id) WHERE reply_message_id IS NOT NULL;

-- Extraction provenance becomes tenant-safe and accepts user assertions only.
ALTER TABLE public.memory_facts ADD COLUMN IF NOT EXISTS learned_at_watermark bigint;
UPDATE public.memory_facts f SET learned_at_watermark=x.max_wm
FROM (
  SELECT e.fact_id,max(tm.source_watermark) max_wm
  FROM public.memory_evidence e
  JOIN public.memory_source_turn_messages tm ON tm.message_id=e.source_id
  GROUP BY e.fact_id
) x WHERE x.fact_id=f.id AND f.learned_at_watermark IS NULL;

ALTER TABLE public.memory_evidence
  ADD COLUMN IF NOT EXISTS user_id uuid,
  ADD COLUMN IF NOT EXISTS source_sender text,
  ADD COLUMN IF NOT EXISTS span_start integer,
  ADD COLUMN IF NOT EXISTS span_end integer,
  ADD COLUMN IF NOT EXISTS extractor_version text NOT NULL DEFAULT 'memory-extract-v1',
  ADD COLUMN IF NOT EXISTS prompt_version text NOT NULL DEFAULT 'memory-prompt-v1';
UPDATE public.memory_evidence e SET
  user_id=f.user_id,
  source_sender=m.sender
FROM public.memory_facts f, public.messages m
WHERE e.fact_id=f.id AND e.source_id=m.id
  AND (e.user_id IS NULL OR e.source_sender IS NULL);
DELETE FROM public.memory_evidence WHERE source_sender<>'user' OR user_id IS NULL;
UPDATE public.memory_facts f SET status='forgotten',valid_to=now(),updated_at=now()
WHERE f.status='active' AND NOT EXISTS (
  SELECT 1 FROM public.memory_evidence e WHERE e.fact_id=f.id
);
ALTER TABLE public.memory_evidence ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE public.memory_evidence ALTER COLUMN source_sender SET NOT NULL;
ALTER TABLE public.memory_evidence DROP CONSTRAINT IF EXISTS memory_evidence_pkey;
ALTER TABLE public.memory_evidence ADD CONSTRAINT memory_evidence_pkey
  PRIMARY KEY (user_id,fact_id,source_type,source_id);

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname='memory_evidence_user_fact_fk'
      AND conrelid='public.memory_evidence'::regclass
  ) THEN
    ALTER TABLE public.memory_evidence ADD CONSTRAINT memory_evidence_user_fact_fk
      FOREIGN KEY (user_id,fact_id) REFERENCES public.memory_facts(user_id,id) ON DELETE CASCADE;
    ALTER TABLE public.memory_evidence ADD CONSTRAINT memory_evidence_user_message_fk
      FOREIGN KEY (user_id,source_id,source_sender)
      REFERENCES public.messages(user_id,id,sender) ON DELETE CASCADE;
    ALTER TABLE public.memory_evidence ADD CONSTRAINT memory_evidence_user_sender_ck
      CHECK (source_sender='user');
    ALTER TABLE public.memory_evidence ADD CONSTRAINT memory_evidence_span_ck CHECK (
      (span_start IS NULL AND span_end IS NULL)
      OR (span_start >= 0 AND span_end > span_start)
    );
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname='memory_facts_quality_ck'
      AND conrelid='public.memory_facts'::regclass
  ) THEN
    ALTER TABLE public.memory_facts ADD CONSTRAINT memory_facts_quality_ck
      CHECK (importance BETWEEN 0 AND 1 AND confidence BETWEEN 0 AND 1);
    ALTER TABLE public.memory_facts ADD CONSTRAINT memory_facts_object_pair_ck
      CHECK ((predicate IS NULL)=(object_json IS NULL));
  END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS memory_facts_active_hash_uq
  ON public.memory_facts(user_id,normalization_version,content_hash) WHERE status='active';

-- Forget markers fence old learning; exact recall suppression is a separate message/span surface.
ALTER TABLE public.memory_forget_markers
  ADD COLUMN IF NOT EXISTS cut_watermark bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS future_learning text NOT NULL DEFAULT 'block';
UPDATE public.memory_forget_markers m
SET cut_watermark=c.memory_source_watermark
FROM public.chat_contexts c
WHERE c.user_id=m.user_id AND m.cut_watermark=0;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname='memory_forget_future_learning_ck'
      AND conrelid='public.memory_forget_markers'::regclass
  ) THEN
    ALTER TABLE public.memory_forget_markers ADD CONSTRAINT memory_forget_future_learning_ck
      CHECK (future_learning IN ('allow','block'));
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.memory_suppression_operations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  cut_watermark bigint NOT NULL CHECK (cut_watermark >= 0),
  future_learning text NOT NULL CHECK (future_learning IN ('allow','block')),
  scope text NOT NULL CHECK (scope IN ('fact','predicate','all')),
  reason text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.memory_recall_suppressions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL,
  operation_id uuid NOT NULL REFERENCES public.memory_suppression_operations(id) ON DELETE CASCADE,
  message_id bigint NOT NULL,
  source_watermark bigint NOT NULL,
  span_start integer,
  span_end integer,
  source_hash text NOT NULL,
  reason text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (user_id,message_id) REFERENCES public.messages(user_id,id) ON DELETE CASCADE,
  CHECK ((span_start IS NULL AND span_end IS NULL)
         OR (span_start >= 0 AND span_end > span_start))
);
CREATE INDEX IF NOT EXISTS memory_recall_suppressions_lookup_idx
  ON public.memory_recall_suppressions(user_id,message_id);
CREATE UNIQUE INDEX IF NOT EXISTS memory_recall_suppressions_operation_uq
  ON public.memory_recall_suppressions(
    user_id,operation_id,message_id,COALESCE(span_start,-1),COALESCE(span_end,-1)
  );

-- 기존 forget marker도 새 exact suppression 표면으로 옮긴다. source closure의 넓은 구간을 그대로
-- 복사하지 않고 marker가 실제 가리킨 fact/predicate/all의 근거 turn만 계산해 collateral 숨김을 막는다.
INSERT INTO public.memory_suppression_operations
  (id,user_id,cut_watermark,future_learning,scope,reason,created_at)
SELECT m.id,m.user_id,m.cut_watermark,m.future_learning,m.scope,'legacy_marker_backfill',m.created_at
FROM public.memory_forget_markers m
ON CONFLICT (id) DO NOTHING;

WITH affected AS (
  SELECT DISTINCT m.user_id,m.id operation_id,tm.source_watermark
  FROM public.memory_forget_markers m
  JOIN public.memory_facts f ON f.user_id=m.user_id AND (
    (m.scope='fact' AND f.id=m.fact_id) OR
    (m.scope='predicate' AND f.predicate=m.predicate)
  )
  JOIN public.memory_evidence e ON e.user_id=f.user_id AND e.fact_id=f.id
  JOIN public.memory_source_turn_messages tm
    ON tm.user_id=e.user_id AND tm.message_id=e.source_id
  WHERE m.scope IN ('fact','predicate') AND tm.source_watermark<=m.cut_watermark
  UNION
  SELECT m.user_id,m.id,tm.source_watermark
  FROM public.memory_forget_markers m
  JOIN public.memory_source_turn_messages tm
    ON tm.user_id=m.user_id AND tm.source_watermark<=m.cut_watermark
  WHERE m.scope='all'
)
INSERT INTO public.memory_recall_suppressions
  (user_id,operation_id,message_id,source_watermark,source_hash,reason)
SELECT a.user_id,a.operation_id,tm.message_id,a.source_watermark,
       encode(digest(COALESCE(msg.content,''),'sha256'),'hex'),'legacy_marker_backfill'
FROM affected a
JOIN public.memory_source_turn_messages tm
  ON tm.user_id=a.user_id AND tm.source_watermark=a.source_watermark
JOIN public.messages msg ON msg.user_id=tm.user_id AND msg.id=tm.message_id
ON CONFLICT DO NOTHING;

-- Episodic projection stores no duplicate raw text; query joins messages after suppression/hash validation.
CREATE TABLE IF NOT EXISTS public.memory_episodic_messages (
  user_id uuid NOT NULL,
  message_id bigint NOT NULL,
  source_watermark bigint NOT NULL,
  content_hash text NOT NULL,
  embedding vector(1536),
  embedding_model text NOT NULL,
  index_version text NOT NULL,
  suppression_generation bigint NOT NULL,
  indexed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id,message_id),
  FOREIGN KEY (user_id,message_id) REFERENCES public.messages(user_id,id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS memory_episodic_embedding_hnsw_idx
  ON public.memory_episodic_messages USING hnsw (embedding vector_cosine_ops)
  WHERE embedding IS NOT NULL;

-- Diary provenance and suppression-safe recall projection.
CREATE TABLE IF NOT EXISTS public.diary_claim_sources (
  user_id uuid NOT NULL,
  diary_id uuid NOT NULL,
  message_id bigint NOT NULL,
  source_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id,diary_id,message_id),
  FOREIGN KEY (user_id,diary_id) REFERENCES public.diaries(user_id,id) ON DELETE CASCADE,
  FOREIGN KEY (user_id,message_id) REFERENCES public.messages(user_id,id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS public.diary_recall_documents (
  user_id uuid NOT NULL,
  diary_id uuid NOT NULL,
  search_text text NOT NULL,
  source_hash text NOT NULL,
  embedding vector(1536),
  suppression_generation bigint NOT NULL,
  index_version text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id,diary_id),
  FOREIGN KEY (user_id,diary_id) REFERENCES public.diaries(user_id,id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS diary_recall_documents_text_trgm_idx
  ON public.diary_recall_documents USING gin (search_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS diary_recall_documents_embedding_hnsw_idx
  ON public.diary_recall_documents USING hnsw (embedding vector_cosine_ops)
  WHERE embedding IS NOT NULL;

-- Persisted public diary cards and short-lived conversational focus.
CREATE TABLE IF NOT EXISTS public.chat_response_references (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL,
  reply_message_id bigint NOT NULL,
  ordinal integer NOT NULL CHECK (ordinal BETWEEN 0 AND 2),
  schema_version text NOT NULL DEFAULT 'diary-reference-v1',
  domain text NOT NULL DEFAULT 'diary' CHECK (domain='diary'),
  mode text NOT NULL CHECK (mode IN ('full_card','reopen_reference')),
  state text NOT NULL DEFAULT 'available' CHECK (state IN ('available','unavailable')),
  diary_id uuid,
  rendered_metadata jsonb NOT NULL DEFAULT '{}',
  redacted_at timestamptz,
  redaction_reason text,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (user_id,reply_message_id) REFERENCES public.messages(user_id,id) ON DELETE CASCADE,
  FOREIGN KEY (user_id,diary_id) REFERENCES public.diaries(user_id,id) ON DELETE RESTRICT,
  UNIQUE (user_id,reply_message_id,ordinal),
  CHECK ((state='available' AND diary_id IS NOT NULL AND redacted_at IS NULL)
         OR (state='unavailable' AND diary_id IS NULL))
);
CREATE INDEX IF NOT EXISTS chat_response_references_reply_idx
  ON public.chat_response_references(user_id,reply_message_id,ordinal);

CREATE TABLE IF NOT EXISTS public.conversation_focus (
  user_id uuid PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  domain text NOT NULL,
  facet text,
  reference_ids uuid[] NOT NULL CHECK (cardinality(reference_ids) BETWEEN 1 AND 3),
  context_revision bigint NOT NULL,
  expires_at timestamptz NOT NULL,
  expires_turn_seq bigint NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Deletion serving barrier is intentionally not FK-bound to profiles so it survives account deletion.
CREATE TABLE IF NOT EXISTS public.privacy_subject_barriers (
  user_id uuid PRIMARY KEY,
  state text NOT NULL CHECK (state IN ('deleting','deleted')),
  operation_id uuid NOT NULL,
  high_watermark bigint,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.privacy_ledger_events (
  id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  operation_id uuid NOT NULL,
  user_id uuid NOT NULL,
  event text NOT NULL,
  high_watermark bigint,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS privacy_ledger_user_idx
  ON public.privacy_ledger_events(user_id,id);

-- Durable job replay lineage and bounded payload retention.
ALTER TABLE public.async_jobs
  ADD COLUMN IF NOT EXISTS replay_of uuid REFERENCES public.async_jobs(id),
  ADD COLUMN IF NOT EXISTS replay_operation_id uuid,
  ADD COLUMN IF NOT EXISTS payload_schema_version text NOT NULL DEFAULT 'job-payload-v1',
  ADD COLUMN IF NOT EXISTS payload_hash text,
  ADD COLUMN IF NOT EXISTS payload_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS payload_redacted_at timestamptz;
UPDATE public.async_jobs SET
  payload_hash=COALESCE(payload_hash,encode(digest(payload::text,'sha256'),'hex')),
  payload_expires_at=COALESCE(payload_expires_at,
    CASE WHEN state='dead' THEN COALESCE(finished_at,created_at)+interval '7 days'
         WHEN state IN ('succeeded','cancelled') THEN COALESCE(finished_at,created_at)+interval '24 hours'
         ELSE NULL END)
WHERE payload_hash IS NULL OR payload_expires_at IS NULL;
CREATE INDEX IF NOT EXISTS async_jobs_replay_of_idx
  ON public.async_jobs(replay_of) WHERE replay_of IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS async_jobs_replay_operation_uq
  ON public.async_jobs(replay_of,replay_operation_id)
  WHERE replay_of IS NOT NULL AND replay_operation_id IS NOT NULL;

-- Composite provenance FKs that were previously app-only.
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname='memory_source_turns_user_message_fk'
      AND conrelid='public.memory_source_turns'::regclass
  ) THEN
    ALTER TABLE public.memory_source_turns ADD CONSTRAINT memory_source_turns_user_message_fk
      FOREIGN KEY (user_id,representative_message_id)
      REFERENCES public.messages(user_id,id) ON DELETE CASCADE;
    ALTER TABLE public.memory_source_turn_messages ADD CONSTRAINT memory_source_turn_messages_user_message_fk
      FOREIGN KEY (user_id,message_id) REFERENCES public.messages(user_id,id) ON DELETE CASCADE;
    ALTER TABLE public.conversation_checkpoints ADD CONSTRAINT conversation_checkpoints_user_message_fk
      FOREIGN KEY (user_id,through_message_id) REFERENCES public.messages(user_id,id) ON DELETE CASCADE;
  END IF;
END $$;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'schema_migrations','chat_active_turns','chat_response_references','conversation_focus',
    'memory_suppression_operations','memory_recall_suppressions','memory_episodic_messages',
    'diary_generation_results',
    'diary_claim_sources','diary_recall_documents','privacy_subject_barriers','privacy_ledger_events'
  ] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY;',t);
    EXECUTE format('REVOKE ALL ON public.%I FROM anon, authenticated;',t);
  END LOOP;
END $$;

COMMIT;
