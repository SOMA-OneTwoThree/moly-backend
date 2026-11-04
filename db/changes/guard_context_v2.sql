-- 압박 시 저장 완료 대화를 정리할 포함 경계. 기존 개발 Supabase만 별도 검토 후 적용.
-- db.apply의 단일 트랜잭션에서 실행하며 원문/기억/기존 작업은 수정·삭제하지 않는다.
-- 배포 순서: 이 SQL → 개발 catalog 정식 재생성·검증 → 새 코드. 운영 자동 적용 금지.
-- 복구: 먼저 기능 off. 실행 중 구버전/신버전 writer가 없음을 확인한 뒤에만
-- 두 trigger/function과 CHECK/컬럼을 역순 삭제한다. 목표·진행 상태 보존이 기본이다.

ALTER TABLE public.chat_contexts
  ADD COLUMN IF NOT EXISTS retention_pressure_through_message_id bigint;
DO $pressure_constraint$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
    WHERE conrelid='public.chat_contexts'::regclass
      AND conname='chat_contexts_retention_pressure_positive_check') THEN
    ALTER TABLE public.chat_contexts
      ADD CONSTRAINT chat_contexts_retention_pressure_positive_check
      CHECK (retention_pressure_through_message_id IS NULL OR retention_pressure_through_message_id>0)
      NOT VALID;
  END IF;
END
$pressure_constraint$;
ALTER TABLE public.chat_contexts VALIDATE CONSTRAINT chat_contexts_retention_pressure_positive_check;

CREATE OR REPLACE FUNCTION public.reset_retention_pressure_generation() RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $function$
BEGIN
  IF NEW.memory_generation IS DISTINCT FROM OLD.memory_generation THEN
    NEW.retention_pressure_through_message_id := NULL;
  END IF;
  RETURN NEW;
END;
$function$;
REVOKE ALL ON FUNCTION public.reset_retention_pressure_generation() FROM PUBLIC, anon, authenticated, service_role;
DROP TRIGGER IF EXISTS chat_contexts_retention_pressure_generation ON public.chat_contexts;
CREATE TRIGGER chat_contexts_retention_pressure_generation BEFORE UPDATE OF memory_generation ON public.chat_contexts
FOR EACH ROW EXECUTE FUNCTION public.reset_retention_pressure_generation();

CREATE OR REPLACE FUNCTION public.reset_retention_pressure_privacy() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO ''
AS $function$
BEGIN
  IF NEW.state <> 'active' OR (TG_OP = 'UPDATE' AND NEW.epoch IS DISTINCT FROM OLD.epoch) THEN
    UPDATE public.chat_contexts SET retention_pressure_through_message_id = NULL
    WHERE user_id = NEW.user_id AND retention_pressure_through_message_id IS NOT NULL;
  END IF;
  RETURN NEW;
END;
$function$;
REVOKE ALL ON FUNCTION public.reset_retention_pressure_privacy() FROM PUBLIC, anon, authenticated, service_role;
DROP TRIGGER IF EXISTS privacy_subject_barriers_retention_pressure ON public.privacy_subject_barriers;
CREATE TRIGGER privacy_subject_barriers_retention_pressure AFTER INSERT OR UPDATE OF state, epoch ON public.privacy_subject_barriers
FOR EACH ROW EXECUTE FUNCTION public.reset_retention_pressure_privacy();

-- cursor는 sweep가 별도 짧은 트랜잭션에서 CAS로 최초 생성한다. 사용자/작업 lock과 함께 잡지 않는다.
-- 적용 확인(읽기 전용): 컬럼 bigint nullable, 위 이름 CHECK validated,
-- 위 trigger 2개 enabled O, 함수 search_path='', PUBLIC 실행 권한 없음.
-- 초기 목표는 NULL. 실제 원문은 모든 성공·실패·off/on 경로에서 보존한다.
