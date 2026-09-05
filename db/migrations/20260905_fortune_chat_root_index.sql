-- 운세 대화가 활성화된 동안 일반 채팅도 가장 최근 fortune_context_root를 확인한다.
-- 운세 대화를 한 적 없는 사용자의 전체 메시지 인덱스를 뒤에서 훑지 않도록 root 행만 인덱싱한다.
--
-- CREATE INDEX CONCURRENTLY는 트랜잭션 블록 안에서 실행할 수 없다. dev/prod 모두
-- db/RUNBOOK_PROD_DDL.md 절차로 psql 직결 실행 후 schema_migrations에 checksum을 기록한다.
CREATE INDEX CONCURRENTLY IF NOT EXISTS messages_fortune_context_root_idx
  ON public.messages (user_id, id DESC)
  WHERE sender = 'user' AND kind = 'fortune_context_root';
