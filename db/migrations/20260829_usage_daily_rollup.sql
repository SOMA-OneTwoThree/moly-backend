-- 적용: python db/apply.py db/migrations/20260829_usage_daily_rollup.sql --env prod --commit --allow-prod
-- Phase 5-2(로드맵 v4): ai_usage_ledger 90일 이전 completed/failed 행의 일 단위 롤업 보관처.
--
-- 키 축은 **KST date**다 — activity_date는 74% NULL(백그라운드 lane이 안 넣음)이라 금지(§9.6).
-- (kst_date, provider, model, lane, purpose, status) 전부 원장에서 NOT NULL 유래라 PK에 NULL 불가.
-- status 축을 넣어 failed 카운트를 보존한다(불변식 2 — 실패도 비용 관측의 일부).
-- 값 컬럼은 **가산형**(retention 잡의 ON CONFLICT … SET calls = calls + EXCLUDED.calls)이라
-- 어느 지점에서 배치가 죽어도 재실행이 각 원본 행을 정확히 1회만 반영한다(단일 문장 원자성).
-- unknown_usage·started 행은 여기로 오지 않는다 — 삭제 자체가 금지(원장에 원본 보존).
CREATE TABLE IF NOT EXISTS public.ai_usage_daily_rollup (
    kst_date date NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    lane text NOT NULL,
    purpose text NOT NULL,
    status text NOT NULL,
    calls bigint NOT NULL DEFAULT 0,
    input_tokens bigint NOT NULL DEFAULT 0,
    cached_input_tokens bigint NOT NULL DEFAULT 0,
    cache_write_tokens bigint NOT NULL DEFAULT 0,
    output_tokens bigint NOT NULL DEFAULT 0,
    embedding_tokens bigint NOT NULL DEFAULT 0,
    cost_micro_usd bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kst_date, provider, model, lane, purpose, status)
);
