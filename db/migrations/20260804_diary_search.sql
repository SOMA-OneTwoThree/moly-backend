-- 적용: python db/apply.py db/migrations/20260804_diary_search.sql --commit
-- 한국어 일기는 형태소 사전 없는 tsvector보다 pg_trgm 부분문자열 검색을 사용한다.
BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS diaries_content_trgm_idx
  ON public.diaries USING gin (content gin_trgm_ops);

COMMIT;
