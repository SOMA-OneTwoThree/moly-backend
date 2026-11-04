-- 최초 운영 전환 2/6: 이 검증 하나만 별도 TX로 실행한다.
-- ADD CONSTRAINT/이름 교체와 같은 TX에 합치지 않는다.
-- SHARE UPDATE EXCLUSIVE 검증은 일반 SELECT/INSERT/UPDATE/DELETE와 호환된다.
-- db.apply --statement-timeout-ms 60000, 실패 시 이 TX만 rollback한다.
ALTER TABLE public.messages VALIDATE CONSTRAINT messages_kind_guardrails_check;
