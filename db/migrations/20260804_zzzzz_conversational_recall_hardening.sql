-- Corrective convergence after the existing-user recall backfill.
-- Keeps raw text in records, excludes deleting subjects, and adds bounded vector repair state.
BEGIN;

ALTER TABLE public.memory_episodic_messages
  ADD COLUMN IF NOT EXISTS embedding_repair_attempts smallint NOT NULL DEFAULT 0;
ALTER TABLE public.diary_recall_documents
  ADD COLUMN IF NOT EXISTS embedding_repair_attempts smallint NOT NULL DEFAULT 0;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='memory_episode_repair_attempts_ck') THEN
    ALTER TABLE public.memory_episodic_messages ADD CONSTRAINT memory_episode_repair_attempts_ck
      CHECK (embedding_repair_attempts BETWEEN 0 AND 3);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='diary_recall_repair_attempts_ck') THEN
    ALTER TABLE public.diary_recall_documents ADD CONSTRAINT diary_recall_repair_attempts_ck
      CHECK (embedding_repair_attempts BETWEEN 0 AND 3);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS memory_episodic_missing_embedding_idx
  ON public.memory_episodic_messages(indexed_at) WHERE embedding IS NULL;
CREATE INDEX IF NOT EXISTS diary_recall_missing_embedding_idx
  ON public.diary_recall_documents(updated_at) WHERE embedding IS NULL;

-- A deletion barrier wins over every projection/backfill producer. Raw record deletion remains the
-- account-deletion workflow's responsibility, except a system welcome created after that barrier.
DELETE FROM public.diary_recall_documents rd
USING public.privacy_subject_barriers b WHERE b.user_id=rd.user_id;
DELETE FROM public.memory_episodic_messages e
USING public.privacy_subject_barriers b WHERE b.user_id=e.user_id;
DELETE FROM public.diary_claim_sources s
USING public.privacy_subject_barriers b WHERE b.user_id=s.user_id;
DELETE FROM public.diaries d
USING public.privacy_subject_barriers b
WHERE b.user_id=d.user_id AND d.kind='welcome' AND d.source='welcome'
  AND d.created_at>=b.created_at;

-- Provenance is an exact rebuildable set, not an append-only guess. shared_day may only use user
-- messages that existed when the diary row was created; later same-day turns are not its claims.
WITH expected AS (
  SELECT d.user_id,d.id AS diary_id,m.id AS message_id
  FROM public.diaries d
  JOIN LATERAL (
    SELECT msg.id FROM public.messages msg
    WHERE msg.user_id=d.user_id AND msg.sender='user' AND msg.created_at<=d.created_at
    ORDER BY msg.id LIMIT 1
  ) m ON true
  WHERE d.kind='welcome' AND d.record_status='published' AND d.deleted_at IS NULL
    AND NOT EXISTS (SELECT 1 FROM public.privacy_subject_barriers b WHERE b.user_id=d.user_id)
  UNION ALL
  SELECT d.user_id,d.id,m.id
  FROM public.diaries d
  JOIN public.messages m ON m.user_id=d.user_id AND m.sender='user'
    AND m.activity_date=d.activity_date AND m.created_at<=d.created_at
  WHERE d.kind='shared_day' AND d.record_status='published' AND d.deleted_at IS NULL
    AND NOT EXISTS (SELECT 1 FROM public.privacy_subject_barriers b WHERE b.user_id=d.user_id)
)
DELETE FROM public.diary_claim_sources s
USING public.diaries d
WHERE d.user_id=s.user_id AND d.id=s.diary_id AND d.kind IN ('welcome','shared_day')
  AND NOT EXISTS (
    SELECT 1 FROM expected e
    WHERE e.user_id=s.user_id AND e.diary_id=s.diary_id AND e.message_id=s.message_id
  );

WITH expected AS (
  SELECT d.user_id,d.id AS diary_id,m.id AS message_id,m.content
  FROM public.diaries d
  JOIN LATERAL (
    SELECT msg.id,msg.content FROM public.messages msg
    WHERE msg.user_id=d.user_id AND msg.sender='user' AND msg.created_at<=d.created_at
    ORDER BY msg.id LIMIT 1
  ) m ON true
  WHERE d.kind='welcome' AND d.record_status='published' AND d.deleted_at IS NULL
    AND NOT EXISTS (SELECT 1 FROM public.privacy_subject_barriers b WHERE b.user_id=d.user_id)
  UNION ALL
  SELECT d.user_id,d.id,m.id,m.content
  FROM public.diaries d
  JOIN public.messages m ON m.user_id=d.user_id AND m.sender='user'
    AND m.activity_date=d.activity_date AND m.created_at<=d.created_at
  WHERE d.kind='shared_day' AND d.record_status='published' AND d.deleted_at IS NULL
    AND NOT EXISTS (SELECT 1 FROM public.privacy_subject_barriers b WHERE b.user_id=d.user_id)
)
INSERT INTO public.diary_claim_sources(user_id,diary_id,message_id,source_hash)
SELECT user_id,diary_id,message_id,encode(digest(COALESCE(content,''),'sha256'),'hex')
FROM expected
ON CONFLICT (user_id,diary_id,message_id) DO UPDATE SET source_hash=EXCLUDED.source_hash;

-- Remove stale documents before rebuilding eligible published diaries. SHA-256 is the shared stale
-- write/dedup fence. A changed hash/model/index invalidates only the rebuildable vector.
DELETE FROM public.diary_recall_documents rd
WHERE NOT EXISTS (
  SELECT 1 FROM public.diaries d
  WHERE d.user_id=rd.user_id AND d.id=rd.diary_id
    AND d.kind IN ('welcome','shared_day','capi_day')
    AND d.record_status='published' AND d.deleted_at IS NULL
    AND NOT EXISTS (SELECT 1 FROM public.privacy_subject_barriers b WHERE b.user_id=d.user_id)
);

INSERT INTO public.diary_recall_documents
  (user_id,diary_id,search_text,source_hash,embedding_model,
   suppression_generation,index_version,updated_at)
SELECT d.user_id,d.id,trim(concat_ws(E'\n',d.title,d.content)),
       encode(digest(concat_ws(E'\n',d.title,d.content),'sha256'),'hex'),
       'text-embedding-3-small',COALESCE(c.memory_generation,0),'diary-recall-v1',now()
FROM public.diaries d
LEFT JOIN public.chat_contexts c ON c.user_id=d.user_id
WHERE d.kind IN ('welcome','shared_day','capi_day')
  AND d.record_status='published' AND d.deleted_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM public.privacy_subject_barriers b WHERE b.user_id=d.user_id)
ON CONFLICT (user_id,diary_id) DO UPDATE SET
  search_text=EXCLUDED.search_text,source_hash=EXCLUDED.source_hash,
  embedding_model=EXCLUDED.embedding_model,
  suppression_generation=EXCLUDED.suppression_generation,index_version=EXCLUDED.index_version,
  embedding_repair_attempts=CASE
    WHEN diary_recall_documents.source_hash IS DISTINCT FROM EXCLUDED.source_hash
      OR diary_recall_documents.embedding_model IS DISTINCT FROM EXCLUDED.embedding_model
      OR diary_recall_documents.index_version IS DISTINCT FROM EXCLUDED.index_version
    THEN 0 ELSE diary_recall_documents.embedding_repair_attempts END,
  embedding=CASE
    WHEN diary_recall_documents.source_hash IS DISTINCT FROM EXCLUDED.source_hash
      OR diary_recall_documents.embedding_model IS DISTINCT FROM EXCLUDED.embedding_model
      OR diary_recall_documents.index_version IS DISTINCT FROM EXCLUDED.index_version
    THEN NULL ELSE diary_recall_documents.embedding END,
  updated_at=now();

-- Remove episode metadata that no longer has an eligible owned user-message source.
DELETE FROM public.memory_episodic_messages e
WHERE NOT EXISTS (
  SELECT 1 FROM public.messages m
  JOIN public.memory_source_turn_messages tm
    ON tm.user_id=m.user_id AND tm.message_id=m.id
  WHERE m.user_id=e.user_id AND m.id=e.message_id AND m.sender='user'
    AND NOT EXISTS (SELECT 1 FROM public.privacy_subject_barriers b WHERE b.user_id=m.user_id)
);

-- Initial repair jobs are eligible rows with missing vectors only. Runtime reconciliation creates
-- distinct bounded repair keys if any terminal job later leaves a vector missing.
INSERT INTO public.async_jobs
  (queue,job_type,user_id,dedup_key,payload,payload_hash,payload_schema_version,max_attempts)
SELECT 'content','diary_recall_embed',rd.user_id,
       'diary-recall-v1:' || rd.embedding_model || ':' || rd.diary_id::text || ':' ||
       rd.source_hash || ':' || rd.suppression_generation::text,
       p.payload,encode(digest(p.payload::text,'sha256'),'hex'),'diary-recall-v1',3
FROM public.diary_recall_documents rd
JOIN public.diaries d ON d.user_id=rd.user_id AND d.id=rd.diary_id
CROSS JOIN LATERAL (
  SELECT jsonb_build_object('schema_version','diary-recall-v1','diary_id',rd.diary_id::text) payload
) p
WHERE rd.embedding IS NULL AND d.record_status='published' AND d.deleted_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM public.privacy_subject_barriers b WHERE b.user_id=rd.user_id)
ON CONFLICT (job_type,dedup_key) DO NOTHING;

INSERT INTO public.async_jobs
  (queue,job_type,user_id,dedup_key,payload,payload_hash,payload_schema_version,max_attempts)
SELECT 'interactive_async','memory_episode_embed',e.user_id,
       'episode-v1:' || e.embedding_model || ':' || e.user_id::text || ':' ||
       e.message_id::text || ':' || e.content_hash,
       p.payload,encode(digest(p.payload::text,'sha256'),'hex'),'episode-v1',3
FROM public.memory_episodic_messages e
CROSS JOIN LATERAL (
  SELECT jsonb_build_object('schema_version','episode-v1','message_id',e.message_id) payload
) p
WHERE e.embedding IS NULL
  AND NOT EXISTS (SELECT 1 FROM public.privacy_subject_barriers b WHERE b.user_id=e.user_id)
ON CONFLICT (job_type,dedup_key) DO NOTHING;

COMMIT;
