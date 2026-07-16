-- diaries.source 허용 집합에 'welcome' 추가 (additive).
-- 웰컴 일기를 source='welcome'로 생성하는데 기존 CHECK (source IN ('llm','preset'))가
-- 이를 막아 GET /diaries의 lazy 웰컴 생성이 CHECK 위반 → 500. 허용값만 넓힌다.
-- 제약 이름에 의존하지 않도록 source 컬럼"만" 참조하는 CHECK를 찾아 교체한다.
-- (정의 문자열 매칭은 'source'가 우연히 들어간 무관한/복합 제약까지 지워버릴 수 있다.)
BEGIN;

DO $$
DECLARE c record;
BEGIN
  FOR c IN
    SELECT con.conname
    FROM pg_constraint con
    JOIN pg_class rel ON rel.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = rel.relnamespace
    WHERE n.nspname = 'public' AND rel.relname = 'diaries'
      AND con.contype = 'c'
      AND con.conkey = ARRAY[(
        SELECT att.attnum FROM pg_attribute att
        WHERE att.attrelid = rel.oid AND att.attname = 'source'
      )]::smallint[]
  LOOP
    EXECUTE format('ALTER TABLE public.diaries DROP CONSTRAINT %I', c.conname);
  END LOOP;
END $$;

ALTER TABLE public.diaries ADD CONSTRAINT diaries_source_check
  CHECK (source IN ('llm','preset','welcome'));

COMMIT;
