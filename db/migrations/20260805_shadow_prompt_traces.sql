-- 적용: python db/apply.py db/migrations/20260805_shadow_prompt_traces.sql --commit
-- 기억 재설계 9단계 (docs/ARCHITECTURE-capi.md 3.2절) — shadow 프롬프트 계측.
--
-- 새 assembler를 **실제 LLM 응답에는 쓰지 않고** 프롬프트 크기·캐시 가능 비율만 기록한다.
-- 이 설계의 비용 주장("휘발값을 최근 원문 뒤에 두면 앞 prefix가 캐시된다")이 실제 대화
-- 데이터에서 성립하는지 판정할 근거다. 합성 fixture는 이미 통과했지만, 진짜 대화의 길이
-- 분포에서도 성립하는지는 여기서만 알 수 있다.
BEGIN;

CREATE TABLE IF NOT EXISTS public.shadow_prompt_traces (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  turn_seq          bigint NOT NULL CHECK (turn_seq > 0),
  -- assembler 버전. 조립 규칙이 바뀌면 옛 trace와 섞어서 비교하면 안 된다.
  assembler_version text NOT NULL,
  total_bytes       integer NOT NULL CHECK (total_bytes >= 0),
  cacheable_bytes   integer NOT NULL CHECK (cacheable_bytes >= 0),
  volatile_bytes    integer NOT NULL CHECK (volatile_bytes >= 0),
  message_count     integer NOT NULL CHECK (message_count >= 0),
  segment_counts    jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at        timestamptz NOT NULL DEFAULT now(),
  -- 같은 turn을 재계측해도 한 행이다. 재시도가 통계를 부풀리면 안 된다.
  CONSTRAINT shadow_prompt_traces_turn_uniq UNIQUE (user_id, turn_seq, assembler_version)
);

CREATE INDEX IF NOT EXISTS shadow_prompt_traces_user_idx
  ON public.shadow_prompt_traces (user_id, turn_seq);

COMMIT;
