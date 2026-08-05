-- 적용: python db/apply.py db/migrations/20260805_mem0_v2_collection.sql --commit
-- 기억 재설계 (docs/capi-memory-ARCHITECTURE.md 9.1절) — v2 벡터 컬렉션.
--
-- ⚠️ **런타임이 만들지 않는다.** vecs 기본 클라이언트는 `create schema`·`create extension`·
--    `create table`을 런타임에 치는데, 그러면 서비스 롤에 CREATE 권한이 필요해진다.
--    `Mem0VectorIndexAdapter`는 이미 존재하는 컬렉션만 열고, 없으면 실패한다.
--
-- 레이아웃은 vecs 계약을 그대로 따른다(id varchar / vec vector / metadata jsonb).
-- legacy `vecs.memories`는 건드리지 않는다 — 전환 중 두 컬렉션이 공존한다.
BEGIN;

CREATE SCHEMA IF NOT EXISTS vecs;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS vecs.moly_memories_v2 (
  id       varchar PRIMARY KEY,
  vec      vector(1536) NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- 사용자 격리 필터가 매번 풀스캔하지 않게. adapter는 항상 user_id로 거른다.
CREATE INDEX IF NOT EXISTS moly_memories_v2_user_idx
  ON vecs.moly_memories_v2 ((metadata->>'user_id'));

-- 코사인 검색용 HNSW. migration이 만들고 런타임은 만들지 않는다.
CREATE INDEX IF NOT EXISTS moly_memories_v2_hnsw_idx
  ON vecs.moly_memories_v2 USING hnsw (vec vector_cosine_ops);

COMMIT;
