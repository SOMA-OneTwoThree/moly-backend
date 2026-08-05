-- 적용: python db/apply.py db/migrations/20260805_ai_usage_ledger.sql --commit
-- 기억 재설계 1단계(docs/capi-memory-ARCHITECTURE.md 15장 1번) — 비용 계측 표면을 먼저 깐다.
-- 구조 전환 전에 배포해 legacy 비용까지 같은 표면으로 재고, 전환 전/후를 같은 지표로 비교한다.
--
-- 핵심 불변식:
--  · 사용자 quota(daily_token_limit)와 회사 원가(USD)는 다른 값이다. quota는 기존 billable weighted
--    unit을 계속 쓰고, 이 원장은 provider 단가 기반 실원가를 따로 적재한다. 섞지 않는다.
--  · 단가는 코드 상수가 아니라 effective-dated catalog다. 원장 행은 계산에 쓴 catalog_version을
--    저장해 가격이 바뀐 뒤에도 과거 비용을 재현한다.
--  · 응답을 잃은 호출을 0원으로 숨기지 않는다. 호출 전 started를 남기고 완료 시 fenced update,
--    유실은 unknown_usage로 보존한다.
--  · cache-write를 provider가 직접 주지 않으면 추정값이며 cache_write_estimated=true로 표시한다.
--    이 값을 "정확한 실비"라고 부르지 않는다.
BEGIN;

-- ─────────────────────────────────────────────────────────────
-- 1. 단가 catalog — effective-dated. 가격 변경은 새 version 행 추가로만 한다.
--    단가 단위 = micro-USD / 1M tokens ($1.00/1M = 1_000_000).
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.ai_price_catalog (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  catalog_version       integer NOT NULL,
  provider              text NOT NULL,
  model                 text NOT NULL,
  -- NULL = 그 모델에 해당 요금이 없음(예: embedding 전용 모델의 output). 0과 구분한다.
  input_micro_usd       bigint NULL CHECK (input_micro_usd >= 0),
  cached_input_micro_usd bigint NULL CHECK (cached_input_micro_usd >= 0),
  cache_write_micro_usd bigint NULL CHECK (cache_write_micro_usd >= 0),
  output_micro_usd      bigint NULL CHECK (output_micro_usd >= 0),
  embedding_micro_usd   bigint NULL CHECK (embedding_micro_usd >= 0),
  source_note           text NULL,
  effective_from        timestamptz NOT NULL,
  effective_to          timestamptz NULL,
  created_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (catalog_version, provider, model),
  CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE INDEX IF NOT EXISTS ai_price_catalog_lookup_idx
  ON public.ai_price_catalog (provider, model, effective_from DESC);

-- ─────────────────────────────────────────────────────────────
-- 2. 호출 원장 — foreground(동기 chat)와 background(잡) 양쪽 전부.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.ai_usage_ledger (
  call_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- 계정 삭제 시 익명화(집계는 남기고 사람과의 연결만 끊는다). 재연결 가능한 subject hash는 두지 않는다.
  user_id           uuid NULL REFERENCES public.profiles(id) ON DELETE SET NULL,
  turn_seq          bigint NULL,
  job_id            uuid NULL,
  activity_date     date NULL,
  lane              text NOT NULL CHECK (lane IN ('foreground','background')),
  purpose           text NOT NULL,
  provider          text NOT NULL,
  model             text NOT NULL,
  model_snapshot    text NULL,          -- alias 호출의 응답 실제 snapshot
  status            text NOT NULL DEFAULT 'started'
    CHECK (status IN ('started','completed','unknown_usage','failed')),
  started_at        timestamptz NOT NULL DEFAULT now(),
  completed_at      timestamptz NULL,
  latency_ms        integer NULL CHECK (latency_ms >= 0),
  input_tokens          integer NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
  cached_input_tokens   integer NOT NULL DEFAULT 0 CHECK (cached_input_tokens >= 0),
  cache_write_tokens    integer NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
  output_tokens         integer NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
  embedding_tokens      integer NOT NULL DEFAULT 0 CHECK (embedding_tokens >= 0),
  -- provider가 cache-write usage를 주지 않아 추정한 경우 true. 실비 주장 금지 표식.
  cache_write_estimated boolean NOT NULL DEFAULT false,
  provider_request_id   text NULL,
  price_catalog_version integer NULL,
  cost_micro_usd        bigint NULL CHECK (cost_micro_usd >= 0),
  -- unknown_usage 행의 catalog 단가 기반 상한 추정(0원으로 숨기지 않기 위해).
  cost_upper_bound_micro_usd bigint NULL CHECK (cost_upper_bound_micro_usd >= 0),
  attempt           integer NOT NULL DEFAULT 1 CHECK (attempt >= 1),
  schema_version    text NULL,
  prompt_version    text NULL,
  -- 강제 유실 fault 실험 행은 정상 completeness 분모에서 빼되 누락시키지 않는다.
  experiment_id     text NULL,
  error_code        text NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  -- 완료 행은 반드시 어떤 catalog로 계산했는지 남긴다.
  CHECK (status <> 'completed' OR price_catalog_version IS NOT NULL)
);

-- 사용자·일자별 원가 집계(전환 전/후 cohort 비교의 기본 조회).
CREATE INDEX IF NOT EXISTS ai_usage_ledger_user_day_idx
  ON public.ai_usage_ledger (user_id, activity_date, lane);
-- 목적·모델별 원가 대시보드.
CREATE INDEX IF NOT EXISTS ai_usage_ledger_purpose_idx
  ON public.ai_usage_ledger (purpose, started_at DESC);
-- 미수렴 행 추적(started/unknown_usage) — completeness 게이트가 매번 풀스캔하지 않게.
CREATE INDEX IF NOT EXISTS ai_usage_ledger_open_idx
  ON public.ai_usage_ledger (status, started_at) WHERE status IN ('started','unknown_usage');
-- invoice 대사는 provider request id로 찾는다.
CREATE INDEX IF NOT EXISTS ai_usage_ledger_request_idx
  ON public.ai_usage_ledger (provider, provider_request_id) WHERE provider_request_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────────
-- 3. 잡 시도 telemetry — async_jobs 행은 현재 상태만 갖는다. 시도별 이력을 따로 남겨
--    retry 분포·lease 상실·dead 도달 경로를 사후 분석할 수 있게 한다.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.job_attempts (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id        uuid NOT NULL REFERENCES public.async_jobs(id) ON DELETE CASCADE,
  attempt       integer NOT NULL CHECK (attempt >= 1),
  queue         text NOT NULL,
  job_type      text NOT NULL,
  worker_id     text NULL,
  lease_token   uuid NULL,
  started_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz NULL,
  duration_ms   integer NULL CHECK (duration_ms >= 0),
  outcome       text NULL
    CHECK (outcome IS NULL OR outcome IN
      ('succeeded','retryable','dead','cancelled','lease_lost','timeout')),
  error_code    text NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (job_id, attempt)
);

CREATE INDEX IF NOT EXISTS job_attempts_queue_idx
  ON public.job_attempts (queue, started_at DESC);
CREATE INDEX IF NOT EXISTS job_attempts_open_idx
  ON public.job_attempts (job_id) WHERE outcome IS NULL;

-- RLS deny-default — 서버(owner 롤)만 접근. 클라 직접 접근 차단.
ALTER TABLE public.ai_price_catalog ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ai_usage_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.job_attempts ENABLE ROW LEVEL SECURITY;

-- ─────────────────────────────────────────────────────────────
-- 4. catalog v1 시드 — 2026-08-05 공개 Standard 가격.
--    출처: OpenAI GPT-5.6 발표/가격, GPT-4.1 mini, text-embedding-3-small 모델 페이지.
--    ⚠️ GPT-5.6의 cache-write 1.25배를 GPT-4.1 mini에 추정 적용하지 않는다(NULL로 둔다).
-- ─────────────────────────────────────────────────────────────
INSERT INTO public.ai_price_catalog
  (catalog_version, provider, model,
   input_micro_usd, cached_input_micro_usd, cache_write_micro_usd, output_micro_usd,
   embedding_micro_usd, source_note, effective_from)
VALUES
  (1, 'openai', 'gpt-5.6-luna',
   1000000, 100000, 1250000, 6000000, NULL,
   '2026-08-05 공개 Standard 가격', '2026-08-05T00:00:00+00:00'),
  (1, 'openai', 'gpt-5.6-terra',
   2500000, 250000, 3125000, 15000000, NULL,
   '2026-08-05 공개 Standard 가격', '2026-08-05T00:00:00+00:00'),
  (1, 'openai', 'gpt-4.1-mini-2025-04-14',
   400000, 100000, NULL, 1600000, NULL,
   'cache write 미지원 — 추정 적용 금지', '2026-08-05T00:00:00+00:00'),
  (1, 'openai', 'text-embedding-3-small',
   NULL, NULL, NULL, NULL, 20000,
   'embedding 전용', '2026-08-05T00:00:00+00:00')
ON CONFLICT (catalog_version, provider, model) DO NOTHING;

COMMIT;
