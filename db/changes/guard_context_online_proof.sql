-- 이미 guard_context_online.sql을 적용한 개발 DB에 처리 근거 저장 구조만 추가한다.
-- 최초 도입용 최종 guard_context_online.sql에도 같은 컬럼/제약이 포함된다.
-- 기존 공개 계약/원문/작업 payload는 수정하지 않는다. db.apply의 단일 TX로 적용한다.
ALTER TABLE public.retention_prepared_contracts
    ADD COLUMN IF NOT EXISTS validation_json jsonb NOT NULL DEFAULT '{}'::jsonb;
DO $prepared_contract_proof$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.retention_prepared_contracts'::regclass
          AND conname = 'retention_prepared_contracts_validation_check') THEN
        ALTER TABLE public.retention_prepared_contracts
            ADD CONSTRAINT retention_prepared_contracts_validation_check
            CHECK (jsonb_typeof(validation_json) = 'object') NOT VALID;
    END IF;
END
$prepared_contract_proof$;
ALTER TABLE public.retention_prepared_contracts
    VALIDATE CONSTRAINT retention_prepared_contracts_validation_check;
