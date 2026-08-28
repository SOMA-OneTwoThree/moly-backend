-- 적용: python db/apply.py db/migrations/20260804_memory_cutover_guard.sql --commit
-- legacy write 차단 트리거(W10, docs/ARCHITECTURE-capi.md 12장 — legacy, 2026-08-06 폐기).
-- **cutover보다 먼저** 배포한다 — 구버전 프로세스가 하나라도 남아 있는 동안 normalized 유저의
-- chat_contexts에 mem0 문자열이 다시 써지면, forget으로 지운 내용이 프롬프트로 되살아난다.
-- 애플리케이션의 mode 분기(`memory_repo.save_legacy_snapshot`)는 같은 규칙의 **두 번째** 방어선일
-- 뿐이고, 마지막 방어선은 이 트리거다(구버전 코드는 mode를 아예 모른다).
--
-- 핵심 불변식:
--  · normalized 행의 `memory_text`·`memory_refreshed_at`은 **항상 NULL**이다. INSERT든 UPDATE든
--    값이 실려 오면 조용히 NULL로 강제한다(에러로 막지 않는다 — 구버전 쓰기 경로가 500으로
--    터지면 대화가 끊긴다. 목표는 "쓰기를 실패시키는 것"이 아니라 "스냅샷이 남지 않는 것"이다).
--  · **downgrade 금지**: OLD가 normalized면 NEW.memory_mode를 normalized로 되돌린다. normalized →
--    legacy로 되돌리는 UPDATE는 코드에도 없고 DB에서도 통하지 않는다(명세 §4 롤백표: 이미 전환된
--    유저는 legacy로 되돌리지 않고 빈-memory fail-open으로 roll-forward한다).
--  · 구버전 제거 후에도 이 트리거를 **지우지 않는다**. 컬럼을 실제로 떨어뜨리는 contract
--    마이그레이션 때까지 유지한다(명세 1310-1311행).
--
-- additive only(기존 테이블·컬럼을 바꾸지 않는다). W8(20260804_memory_normalization.sql)이
-- memory_mode 컬럼을 만든 **다음에** 적용한다.
BEGIN;

-- 하드 가드(2026-08-28 추가): 이 트리거 함수는 chat_contexts의 legacy 컬럼
-- `memory_text`·`memory_refreshed_at`을 참조한다. 그 컬럼이 이미 DROP된 DB(컬럼 제거
-- 마이그레이션 이후의 재적용·신규 부트스트랩)에 그대로 설치하면, 트리거가 모든
-- chat_contexts INSERT/UPDATE에서 "record NEW has no field" 런타임 에러를 내
-- **대화가 전멸**한다. 컬럼이 없으면 파일 전체를 no-op으로 만든다(README 산문이 아니라
-- 파일 자체가 안전해야 한다 — 실행자는 README를 안 읽는다).
DO $cutover_guard$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'chat_contexts'
      AND column_name = 'memory_text'
  ) THEN
    RAISE NOTICE 'memory_cutover_guard: chat_contexts.memory_text 없음 — legacy 스냅샷 시대 종료 후의 DB. no-op.';
    RETURN;
  END IF;

  EXECUTE $install_fn$
    CREATE OR REPLACE FUNCTION public.guard_normalized_memory_snapshot()
    RETURNS trigger LANGUAGE plpgsql AS $fn$
    BEGIN
      IF TG_OP='INSERT' AND NEW.memory_mode='normalized' THEN
        NEW.memory_text := NULL;
        NEW.memory_refreshed_at := NULL;
      ELSIF TG_OP='UPDATE' AND OLD.memory_mode='normalized' THEN
        NEW.memory_mode := 'normalized';   -- downgrade 차단
        NEW.memory_text := NULL;
        NEW.memory_refreshed_at := NULL;
      END IF;
      RETURN NEW;
    END $fn$
  $install_fn$;

  EXECUTE 'DROP TRIGGER IF EXISTS chat_contexts_normalized_snapshot_guard ON public.chat_contexts';
  EXECUTE $install_trg$
    CREATE TRIGGER chat_contexts_normalized_snapshot_guard
    BEFORE INSERT OR UPDATE ON public.chat_contexts
    FOR EACH ROW EXECUTE FUNCTION public.guard_normalized_memory_snapshot()
  $install_trg$;
END $cutover_guard$;

COMMIT;
