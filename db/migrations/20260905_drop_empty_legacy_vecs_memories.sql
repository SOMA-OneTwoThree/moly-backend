-- 런타임이 사용하지 않는 legacy vecs.memories 컬렉션을 제거한다.
-- 데이터가 한 행이라도 있으면 중단하며, CASCADE를 쓰지 않아 예상 밖 의존성도 조용히 지우지 않는다.
BEGIN;

DO $$
DECLARE
  v_rows bigint;
BEGIN
  IF to_regclass('vecs.memories') IS NULL THEN
    RETURN;
  END IF;

  EXECUTE 'SELECT count(*) FROM vecs.memories' INTO v_rows;
  IF v_rows <> 0 THEN
    RAISE EXCEPTION 'vecs.memories is not empty: % rows', v_rows;
  END IF;

  DROP TABLE vecs.memories;
END $$;

DO $$
BEGIN
  IF to_regclass('vecs.memories') IS NOT NULL THEN
    RAISE EXCEPTION 'vecs.memories still exists after drop';
  END IF;
END $$;

COMMIT;
