-- Existing-user data backfill for the conversational recall schema.
-- Raw text remains only in messages/diaries; async job payloads contain stable IDs only.
BEGIN;

-- The projection records the model as well as its dimension-bound index version, so a future
-- model change invalidates only the rebuildable vector and receives a distinct durable job.
ALTER TABLE public.diary_recall_documents
  ADD COLUMN IF NOT EXISTS embedding_model text NOT NULL DEFAULT 'text-embedding-3-small';

-- A relationship origin backfilled from the first user message must have the same deterministic
-- welcome prologue as a newly committed first turn. The partial unique index makes this rerunnable.
INSERT INTO public.diaries (
  user_id,diary_date,kind,activity_date,display_date,title,author,
  occurred_at,occurred_timezone,occurred_timezone_provenance,
  primary_subject,about_tags,source,content,weather,published_at
)
SELECT
  p.id,p.relationship_display_date,'welcome',NULL,p.relationship_display_date,
  CASE split_part(lower(p.language),'-',1)
    WHEN 'ko' THEN '{유저이름}, 첫 만남'
    WHEN 'ja' THEN '{유저이름}、はじめての出会い'
    ELSE '{유저이름}, our first meeting'
  END,
  'capi',p.relationship_started_at,p.relationship_started_timezone,'profile_snapshot',
  'user',ARRAY['user']::text[],'welcome',
  CASE split_part(lower(p.language),'-',1)
    WHEN 'ko' THEN '오늘 {유저이름}과 처음 대화를 나눴다.' || E'\n' || '우리의 첫 대화가 시작된 날이다.'
    WHEN 'ja' THEN '今日、{유저이름}とはじめて話した。' || E'\n' || 'わたしたちの最初の会話が始まった日だ。'
    ELSE 'Today, {유저이름} and I talked for the first time.' || E'\n' ||
         'This is the day our first conversation began.'
  END,
  'sunny',p.relationship_started_at
FROM public.profiles p
WHERE p.relationship_started_at IS NOT NULL
  AND p.relationship_started_timezone IS NOT NULL
  AND p.relationship_display_date IS NOT NULL
ON CONFLICT DO NOTHING;

-- Welcome is grounded by the first committed user assertion. A shared-day diary is grounded by
-- the user messages from its activity date. Capi-only diaries intentionally have no user claim.
WITH first_user AS (
  SELECT DISTINCT ON (m.user_id) m.user_id,m.id,m.content
  FROM public.messages m
  WHERE m.sender='user'
  ORDER BY m.user_id,m.id
)
INSERT INTO public.diary_claim_sources(user_id,diary_id,message_id,source_hash)
SELECT d.user_id,d.id,m.id,encode(digest(COALESCE(m.content,''),'sha256'),'hex')
FROM public.diaries d
JOIN first_user m ON m.user_id=d.user_id
WHERE d.kind='welcome' AND d.record_status='published' AND d.deleted_at IS NULL
ON CONFLICT (user_id,diary_id,message_id) DO UPDATE SET source_hash=EXCLUDED.source_hash;

INSERT INTO public.diary_claim_sources(user_id,diary_id,message_id,source_hash)
SELECT d.user_id,d.id,m.id,encode(digest(COALESCE(m.content,''),'sha256'),'hex')
FROM public.diaries d
JOIN public.messages m
  ON m.user_id=d.user_id AND m.sender='user' AND m.activity_date=d.activity_date
WHERE d.kind='shared_day' AND d.record_status='published' AND d.deleted_at IS NULL
ON CONFLICT (user_id,diary_id,message_id) DO UPDATE SET source_hash=EXCLUDED.source_hash;

-- Search documents are rebuildable projections. Do not duplicate diary text in job payloads.
INSERT INTO public.diary_recall_documents
  (user_id,diary_id,search_text,source_hash,embedding_model,
   suppression_generation,index_version,updated_at)
SELECT d.user_id,d.id,trim(concat_ws(E'\n',d.title,d.content)),
       md5(concat_ws(E'\n',d.title,d.content)),'text-embedding-3-small',COALESCE(c.memory_generation,0),
       'diary-recall-v1',now()
FROM public.diaries d
LEFT JOIN public.chat_contexts c ON c.user_id=d.user_id
WHERE d.kind IN ('welcome','shared_day','capi_day')
  AND d.record_status='published' AND d.deleted_at IS NULL
ON CONFLICT (user_id,diary_id) DO UPDATE SET
  search_text=EXCLUDED.search_text,source_hash=EXCLUDED.source_hash,
  embedding_model=EXCLUDED.embedding_model,
  suppression_generation=EXCLUDED.suppression_generation,index_version=EXCLUDED.index_version,
  embedding=CASE
    WHEN diary_recall_documents.source_hash IS DISTINCT FROM EXCLUDED.source_hash
      OR diary_recall_documents.embedding_model IS DISTINCT FROM EXCLUDED.embedding_model
      OR diary_recall_documents.index_version IS DISTINCT FROM EXCLUDED.index_version
    THEN NULL ELSE diary_recall_documents.embedding END,
  updated_at=now();

INSERT INTO public.async_jobs
  (queue,job_type,user_id,dedup_key,payload,payload_hash,payload_schema_version,max_attempts)
SELECT 'content','diary_recall_embed',d.user_id,
       'diary-recall-v1:' || d.embedding_model || ':' || d.diary_id::text || ':' ||
       d.source_hash || ':' || d.suppression_generation::text,
       jsonb_build_object('schema_version','diary-recall-v1','diary_id',d.diary_id::text),
       encode(digest(jsonb_build_object(
         'schema_version','diary-recall-v1','diary_id',d.diary_id::text
       )::text,'sha256'),'hex'),
       'diary-recall-v1',3
FROM public.diary_recall_documents d
ON CONFLICT (job_type,dedup_key) DO NOTHING;

-- Episode rows contain hash/vector metadata only. Exact quote authority remains messages.
INSERT INTO public.memory_episodic_messages
  (user_id,message_id,source_watermark,content_hash,embedding_model,index_version,
   suppression_generation,indexed_at)
SELECT m.user_id,m.id,tm.source_watermark,
       encode(digest(COALESCE(m.content,''),'sha256'),'hex'),
       'text-embedding-3-small','episode-v1',COALESCE(c.memory_generation,0),now()
FROM public.messages m
JOIN public.memory_source_turn_messages tm
  ON tm.user_id=m.user_id AND tm.message_id=m.id
LEFT JOIN public.chat_contexts c ON c.user_id=m.user_id
WHERE m.sender='user'
ON CONFLICT (user_id,message_id) DO UPDATE SET
  source_watermark=EXCLUDED.source_watermark,content_hash=EXCLUDED.content_hash,
  embedding_model=EXCLUDED.embedding_model,index_version=EXCLUDED.index_version,
  suppression_generation=EXCLUDED.suppression_generation,
  embedding=CASE
    WHEN memory_episodic_messages.content_hash IS DISTINCT FROM EXCLUDED.content_hash
      OR memory_episodic_messages.embedding_model IS DISTINCT FROM EXCLUDED.embedding_model
      OR memory_episodic_messages.index_version IS DISTINCT FROM EXCLUDED.index_version
    THEN NULL ELSE memory_episodic_messages.embedding END,
  indexed_at=now();

INSERT INTO public.async_jobs
  (queue,job_type,user_id,dedup_key,payload,payload_hash,payload_schema_version,max_attempts)
SELECT 'interactive_async','memory_episode_embed',e.user_id,
       'episode-v1:text-embedding-3-small:' || e.user_id::text || ':' ||
       e.message_id::text || ':' || e.content_hash,
       jsonb_build_object('schema_version','episode-v1','message_id',e.message_id),
       encode(digest(jsonb_build_object(
         'schema_version','episode-v1','message_id',e.message_id
       )::text,'sha256'),'hex'),
       'episode-v1',3
FROM public.memory_episodic_messages e
ON CONFLICT (job_type,dedup_key) DO NOTHING;

COMMIT;
