-- ⚠️ psql 직결 전용 — db/apply.py로 실행 금지 (CONCURRENTLY는 트랜잭션 안에서 실행 불가).
-- 절차: db/RUNBOOK_PROD_DDL.md. 2026-08-29 KST prod 기실행 기록(원장 수동 등재).
-- Phase 5 retention 잡 선행 인덱스 3개(로드맵 v4 §Phase 5).
--  · ai_usage_ledger는 (started_at) 평b-tree — ~~activity_date~~는 74% NULL이라 무용(§9.6).
--    rollup 삭제 술어는 KST 자정 경계를 timestamptz 상수로 환산해 이 인덱스를 탄다.
--  · reconciler(stale started)는 기존 ai_usage_ledger_open_idx가 커버(08-29 EXPLAIN 확인) — 추가 불요.
SET lock_timeout = '2s';
SET statement_timeout = 0;
SET application_name = 'ddl-runbook';

CREATE INDEX CONCURRENTLY IF NOT EXISTS async_jobs_finished_gc_idx
  ON public.async_jobs (finished_at)
  WHERE state IN ('succeeded','cancelled');

CREATE INDEX CONCURRENTLY IF NOT EXISTS idempotency_keys_dedupe_gc_idx
  ON public.idempotency_keys (dedupe_expires_at)
  WHERE dedupe_expires_at IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ai_usage_ledger_started_idx
  ON public.ai_usage_ledger (started_at);

-- 사후 검사: INVALID 잔존 0건이어야 함
SELECT n.nspname, c.relname, i.indisvalid
FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE NOT i.indisvalid AND n.nspname IN ('public','vecs');
