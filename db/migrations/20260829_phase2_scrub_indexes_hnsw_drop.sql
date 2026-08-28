-- ⚠️ psql 직결 전용 — db/apply.py로 실행 금지 (CONCURRENTLY는 트랜잭션 안에서 실행 불가).
-- 절차: db/RUNBOOK_PROD_DDL.md. 이 파일은 2026-08-29 00시대 KST에 prod에 psql 직결로
-- 기실행된 문장의 기록이다(원장 수동 등재). 근거·검증: docs/DB_OPTIMIZATION_ROADMAP.md Phase 2.
--
-- 실행 전 게이트(통과 기록):
--  · INVALID 인덱스 0건(public·vecs), 세션 SET lock_timeout='2s', statement_timeout=0
--  · vecs 실제 술어(metadata->'user_id' = …::jsonb) plain EXPLAIN = Seq Scan (HNSW 미사용 재확증, idx_scan=0)
--  · 회상 диф: 유저 3명 × top40 id 순서까지 드랍 전후 완전 일치
-- 결과: 스크럽 쿼리가 두 인덱스 사용(Bitmap/Index Scan 확인), vecs 총 485MB→255MB(-230MB).

-- Phase 2-1: 스크럽 부분 인덱스 2개 (술어는 jobs.py:359-371 스크럽 WHERE의 부분집합 — 함의 관계)
CREATE INDEX CONCURRENTLY IF NOT EXISTS async_jobs_scrub_idx
  ON public.async_jobs (payload_expires_at)
  WHERE payload_redacted_at IS NULL AND payload_expires_at IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idempotency_keys_scrub_idx
  ON public.idempotency_keys (response_expires_at)
  WHERE response IS NOT NULL;

-- Phase 2-2: 사용 이력 0(idx_scan=0)인 HNSW 드랍 — 회상은 seq scan(의미 동일), upsert 지연(최대 60초)의 원인 제거.
-- 롤백: CREATE INDEX CONCURRENTLY moly_memories_v2_hnsw_idx ON vecs.moly_memories_v2 USING hnsw (vec vector_cosine_ops);
DROP INDEX CONCURRENTLY IF EXISTS vecs.moly_memories_v2_hnsw_idx;

-- Phase 5-6 선적용: 잔존 TOAST 팽창(251MB)이 repack(2-3, 보류) 전까지 더 자라지 않게 autovacuum 강화.
ALTER TABLE vecs.moly_memories_v2
  SET (autovacuum_vacuum_scale_factor=0.02, toast.autovacuum_vacuum_scale_factor=0.02);
