-- 일기 생성과 mem0 기억 추출을 분리하기 위한 일별 watermark.
-- 먼저 이 파일을 커밋해 trigger를 활성화한 뒤 states_seed 파일을 별도 커밋한다.
-- 적용: python db/apply.py db/migrations/20260720_memory_ingestion_states.sql --commit

BEGIN;

CREATE TABLE IF NOT EXISTS public.memory_ingestion_states (
  user_id            uuid   NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  activity_date      date   NOT NULL,
  through_message_id bigint NOT NULL DEFAULT 0 CHECK (through_message_id >= 0),
  attempt_count      integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  last_attempted_at  timestamptz,
  completed_at       timestamptz,
  PRIMARY KEY (user_id, activity_date)
);

CREATE INDEX IF NOT EXISTS memory_ingestion_pending_idx
  ON public.memory_ingestion_states
    (last_attempted_at ASC NULLS FIRST, activity_date, user_id)
  WHERE completed_at IS NULL;

CREATE OR REPLACE FUNCTION public.mark_memory_ingestion_pending()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
  INSERT INTO public.memory_ingestion_states (user_id, activity_date)
  VALUES (NEW.user_id, NEW.activity_date)
  ON CONFLICT (user_id, activity_date) DO UPDATE
  SET completed_at = NULL,
      last_attempted_at = NULL,
      attempt_count = 0;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS messages_mark_memory_ingestion_pending ON public.messages;
CREATE TRIGGER messages_mark_memory_ingestion_pending
  AFTER INSERT ON public.messages
  FOR EACH ROW
  WHEN (NEW.kind = 'normal' AND NEW.sender = 'moly')
  EXECUTE FUNCTION public.mark_memory_ingestion_pending();

ALTER TABLE public.memory_ingestion_states ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.memory_ingestion_states FROM anon, authenticated;

COMMIT;
