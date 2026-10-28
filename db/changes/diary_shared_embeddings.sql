-- Diary vector sharing: additive expansion only; no data or configuration changes.
-- Reviewed one-time release delta, executed with db.apply (default rollback).
-- Apply before compatible readers/writers; retain old private vectors until verified.
-- Objects must be absent. An unexpected existing object aborts rather than being trusted.
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
CREATE TABLE public.diary_template_embeddings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_hash text NOT NULL,
    embedding_model text NOT NULL,
    index_version text NOT NULL,
    dimensions integer DEFAULT 1536 NOT NULL,
    search_text text NOT NULL,
    embedding public.vector(1536),
    embedding_repair_attempts integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT diary_template_embeddings_pkey PRIMARY KEY (id),
    CONSTRAINT diary_template_embeddings_key_uq UNIQUE (source_hash, embedding_model, index_version, dimensions),
    CONSTRAINT diary_template_embeddings_dimensions_ck CHECK ((dimensions = 1536)),
    CONSTRAINT diary_template_embeddings_repair_ck CHECK ((embedding_repair_attempts >= 0))
);

ALTER TABLE public.diary_recall_documents ADD COLUMN shared_embedding_id uuid;
CREATE INDEX diary_recall_shared_embedding_idx ON public.diary_recall_documents USING btree (shared_embedding_id) WHERE (shared_embedding_id IS NOT NULL);
CREATE INDEX diary_template_embeddings_pending_idx ON public.diary_template_embeddings USING btree (updated_at, id) WHERE (embedding IS NULL);
ALTER TABLE public.diary_template_embeddings ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.diary_template_embeddings FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.diary_template_embeddings TO service_role;
-- Validate previous/new schema contracts before mode changes or data backfill.
-- No FK/CHECK on the existing projection: preserve old-image additive compatibility.
