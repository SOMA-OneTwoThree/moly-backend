-- 회원탈퇴 시 mem0의 주 기억·엔티티 컬렉션을 행 제한 없이 함께 삭제하는 서비스 롤 RPC.
-- vecs 컬렉션은 mem0가 lazy 생성하므로 아직 없는 테이블은 건너뛴다.
BEGIN;

CREATE OR REPLACE FUNCTION public.delete_memory_artifacts(p_user_id uuid)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
  v_deleted bigint := 0;
  v_rows bigint;
BEGIN
  IF to_regclass('vecs.memories') IS NOT NULL THEN
    DELETE FROM vecs.memories
    WHERE metadata @> jsonb_build_object('user_id', p_user_id::text);
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_deleted := v_deleted + v_rows;
  END IF;

  IF to_regclass('vecs.memories_entities') IS NOT NULL THEN
    DELETE FROM vecs.memories_entities
    WHERE metadata @> jsonb_build_object('user_id', p_user_id::text);
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_deleted := v_deleted + v_rows;
  END IF;

  RETURN v_deleted;
END;
$$;

REVOKE ALL ON FUNCTION public.delete_memory_artifacts(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.delete_memory_artifacts(uuid) TO service_role;

COMMIT;
