-- 적용: python db/apply.py db/migrations/20260804_zz_memory_contract.sql --commit
-- 정규화 기억 백필·검증이 끝난 뒤에만 이전 기억 컬럼과 보호 트리거를 제거한다.
-- 파일명을 zz로 두어 같은 날짜의 expand 마이그레이션보다 반드시 뒤에 검토되게 한다.
BEGIN;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM public.messages m
    WHERE m.sender='user' AND NOT EXISTS (
      SELECT 1 FROM public.memory_source_turn_messages tm
      WHERE tm.user_id=m.user_id AND tm.message_id=m.id
    )
  ) THEN
    RAISE EXCEPTION 'memory contract blocked: unmapped inbound messages remain';
  END IF;

  IF EXISTS (
    SELECT 1 FROM public.async_jobs
    WHERE job_type IN ('memory_extract','memory_reconcile','memory_embed','relationship_profile_refresh')
      AND state IN ('ready','running','dead')
  ) THEN
    RAISE EXCEPTION 'memory contract blocked: pending/running/dead memory jobs remain';
  END IF;

  IF EXISTS (
    SELECT 1 FROM public.memory_facts WHERE status='active' AND embedding IS NULL
  ) THEN
    RAISE EXCEPTION 'memory contract blocked: active facts without embeddings remain';
  END IF;

  IF EXISTS (
    SELECT 1 FROM public.memory_facts f
    WHERE f.status='active' AND NOT EXISTS (
      SELECT 1 FROM public.relationship_profiles rp
      WHERE rp.user_id=f.user_id AND rp.status='published'
    )
  ) THEN
    RAISE EXCEPTION 'memory contract blocked: users with active facts lack a published profile';
  END IF;
END $$;

DROP TRIGGER IF EXISTS chat_contexts_normalized_snapshot_guard ON public.chat_contexts;
DROP FUNCTION IF EXISTS public.guard_normalized_memory_snapshot();

ALTER TABLE public.chat_contexts
  DROP COLUMN IF EXISTS memory_text,
  DROP COLUMN IF EXISTS memory_refreshed_at,
  DROP COLUMN IF EXISTS memory_mode;

COMMIT;
