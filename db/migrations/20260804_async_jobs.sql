-- 적용: python db/apply.py db/migrations/20260804_async_jobs.sql --commit
-- 잡 플랫폼(W7, docs/ARCHITECTURE-capi.md 9장) — 대화 후속 처리(기억 추출 등)를 내구 큐로.
-- 큐 5종(critical/interactive_async/content/notification/maintenance)은 별도 스키마가 아니라
-- queue 컬럼 값이며, 소비자 내부 슬롯으로만 분리한다(프로세스 분할 없음).
--
-- 핵심 불변식:
--  · attempt는 **claim 시점**에 증가 → 크래시로 finalize 못 한 잡도 재클레임마다 카운트돼 반드시
--    dead에 도달한다(poison job 무한루프 방지).
--  · finalize/heartbeat은 (id, state='running', lease_owner, lease_token) fencing 조건으로만 쓴다 →
--    lease를 잃고 늦게 돌아온 소비자가 잘못 확정하지 못한다.
--  · succeeded/dead/cancelled에서 같은 행을 ready로 되살리지 않는다. 운영 replay는
--    dedup_key='replay:{old_job_id}:{operation_id}'인 **새 행**으로만 한다. dead 자동 삭제 없음.
BEGIN;

CREATE TABLE IF NOT EXISTS public.async_jobs (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  queue         text NOT NULL,
  job_type      text NOT NULL,
  user_id       uuid NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  dedup_key     text NOT NULL,
  payload       jsonb NOT NULL,
  state         text NOT NULL DEFAULT 'ready'
    CHECK (state IN ('ready','running','succeeded','dead','cancelled')),
  priority      integer NOT NULL DEFAULT 100,
  available_at  timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NULL,
  attempt       integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  max_attempts  integer NOT NULL CHECK (max_attempts > 0),
  lease_owner   text NULL,
  lease_token   uuid NULL,
  lease_until   timestamptz NULL,
  result_code   text NULL,
  result_detail jsonb NULL,
  last_error_code text NULL,
  last_error_at timestamptz NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz NULL,
  -- (job_type, dedup_key) = 멱등 키. 구·신 producer가 겹치는 이관 기간에도 ON CONFLICT DO NOTHING으로 합쳐진다.
  UNIQUE (job_type, dedup_key),
  -- lease 3종은 running일 때만 존재(전부 있거나 전부 없거나) — 반쯤 풀린 lease를 스키마가 막는다.
  CHECK (
    (state = 'running' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
    OR (state <> 'running' AND lease_owner IS NULL AND lease_token IS NULL AND lease_until IS NULL)
  )
);

-- claim 후보 스캔·정렬(ready만). 부분 인덱스라 succeeded/dead 누적이 claim 비용에 안 실린다.
CREATE INDEX IF NOT EXISTS async_jobs_claim_idx
  ON public.async_jobs (queue, priority, available_at, created_at) WHERE state='ready';
-- reaper의 만료 lease 회수 스캔(running만).
CREATE INDEX IF NOT EXISTS async_jobs_reclaim_idx
  ON public.async_jobs (queue, lease_until) WHERE state='running';
-- /health/queues의 큐별 ready/running/dead 집계·oldest dead age(이관 게이트 지표).
-- 명세 DDL에 없는 추가분 — dead는 자동 삭제하지 않아 테이블이 단조 증가하므로 게이트 조회가
-- 매번 풀스캔이 되지 않게 한다(additive, 다른 경로 영향 없음).
CREATE INDEX IF NOT EXISTS async_jobs_state_queue_idx
  ON public.async_jobs (state, queue);

-- RLS deny-default(다른 테이블과 동일 불변식) — 서버는 owner 롤이라 우회, 클라(anon/authenticated)
-- 직접 접근 차단. 없으면 악의적 클라가 임의 잡을 INSERT/변조해 백그라운드 처리를 조작할 수 있다.
ALTER TABLE public.async_jobs ENABLE ROW LEVEL SECURITY;

COMMIT;
