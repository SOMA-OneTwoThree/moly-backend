-- ⚠️ psql 직결 전용 — db/apply.py로 실행 금지 (CONCURRENTLY는 트랜잭션 안에서 실행 불가).
-- 절차: db/RUNBOOK_PROD_DDL.md. 2026-08-29 00시대 KST prod에 기실행된 문장의 기록(원장 수동 등재).
-- 근거·검증: docs/DB_OPTIMIZATION_ROADMAP.md Phase 3 (#4→#10→#9), 교차검증 2렌즈(실행 정합·의미 보존) 통과.
--
-- 실행 전 게이트(통과 기록):
--  · #4: 복합 CASCADE FK(conversation_checkpoints_user_message_fk) 실재 재확인. 개별 messages DELETE 경로 코드 0곳,
--    23503 의존 코드 0곳 — RESTRICT가 막아주던 라이브 시나리오 부재.
--  · #10 provider_claim(idx_scan 1.67M): eligible_at/provider/model/lane 참조 repo 전체 0곳(ORM 모델 컬럼 미정의 포함).
--    claim 쿼리 EXPLAIN = async_jobs_claim_idx. 1.67M은 접두사(queue,priority) 겹침의 플래너 선택.
--  · #10 reply_idx: 동일 키 UNIQUE 존재(드랍 후 EXPLAIN = UNIQUE Index Only Scan 확인).
--  · #10 messages_user_id_desc_idx: (user_id, id DESC) 조회 EXPLAIN = messages_user_id_id_sender_uq Backward IOS.
--  · #10 diary 2건: idx_scan=0 + recall_diaries.py 구조상 액세스 경로 불가(행별 표현식 평가) — 회상 결과 불변.
--  · #9: 이름 충돌 0, INVALID 잔존 0(사전·사후), 컬럼 순서 = FK 실정의 접두사 일치,
--    partial WHERE는 RI 내부 조회(col=$1)가 IS NOT NULL 함의라 사용 가능.
-- 실행 결과: 전문 성공, 사후 INVALID 0건, claim 플랜 불변, 탈퇴 CASCADE 경로 messages 참조 FK 전수 인덱스 커버.

SET lock_timeout = '2s';
SET statement_timeout = 0;
SET application_name = 'ddl-runbook';

-- ── #4: 단일컬럼 RESTRICT FK 제거 (복합 CASCADE 유지) — 양쪽 AE 락, 단일 문장이라 원자적
ALTER TABLE public.conversation_checkpoints
  DROP CONSTRAINT conversation_checkpoints_through_message_id_fkey;

-- ── #10: 드랍 5문
DROP INDEX CONCURRENTLY public.async_jobs_provider_claim_idx;
DROP INDEX CONCURRENTLY public.messages_user_id_desc_idx;
DROP INDEX CONCURRENTLY public.chat_response_references_reply_idx;
DROP INDEX CONCURRENTLY public.diary_recall_documents_embedding_hnsw_idx;
DROP INDEX CONCURRENTLY public.diary_recall_documents_text_trgm_idx;

-- ── #9: FK 인덱스 8문 (CIC)
CREATE INDEX CONCURRENTLY IF NOT EXISTS mem0_memory_sources_user_msg_idx
  ON public.mem0_memory_sources (user_id, source_message_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS mem0_ingest_candidate_sources_user_msg_idx
  ON public.mem0_ingest_candidate_sources (user_id, source_message_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS diary_claim_sources_user_msg_idx
  ON public.diary_claim_sources (user_id, message_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS greetings_committed_message_idx
  ON public.greetings (committed_message_id) WHERE committed_message_id IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS async_jobs_user_idx
  ON public.async_jobs (user_id) WHERE user_id IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS routine_completions_user_actdate_idx
  ON public.routine_completions (user_id, activity_date);
CREATE INDEX CONCURRENTLY IF NOT EXISTS hay_transactions_user_id_idx
  ON public.hay_transactions (user_id, id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS mem0_memory_registry_delete_scan_idx
  ON public.mem0_memory_registry (provider_delete_state)
  WHERE provider_delete_state IN ('pending','failed');
