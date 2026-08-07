-- 적용: python db/apply.py db/migrations/20260804_job_replay_lineage.sql --commit
-- terminal 원본을 변경·삭제하지 않고 새 replay 잡의 계보를 감사 가능하게 연결한다.
BEGIN;

ALTER TABLE public.async_jobs
  ADD COLUMN IF NOT EXISTS replay_of uuid NULL REFERENCES public.async_jobs(id);

CREATE INDEX IF NOT EXISTS async_jobs_replay_of_idx
  ON public.async_jobs (replay_of) WHERE replay_of IS NOT NULL;

COMMIT;
