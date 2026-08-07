-- 적용: python db/apply.py db/migrations/20260804_memory_embeddings.sql --commit
-- memory_facts의 검색용 파생 임베딩을 text-embedding-3-small 1536차원으로 고정한다.
-- 진실 소스는 canonical_text이며 embedding은 언제든 재생성할 수 있다.
BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE public.memory_facts
  ALTER COLUMN embedding TYPE vector(1536)
  USING embedding::vector(1536);

CREATE INDEX IF NOT EXISTS memory_facts_embedding_hnsw_idx
  ON public.memory_facts USING hnsw (embedding vector_cosine_ops)
  WHERE status='active' AND embedding IS NOT NULL;

COMMIT;
