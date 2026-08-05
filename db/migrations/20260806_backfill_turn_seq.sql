-- 적용: PYTHONPATH=. uv run python db/apply.py db/migrations/20260806_backfill_turn_seq.sql --commit
--
-- 과거 대화에 턴 좌표를 매긴다.
--
-- `messages.turn_seq`는 nullable로 추가만 됐고 **기존 행을 채우는 코드가 없었다.** 기억
-- 파이프라인의 모든 조회는 `turn_seq IS NOT NULL`로 거른다(memory_pipeline `_NEXT_INGEST`).
-- 그래서 좌표 없는 메시지는 영원히 기억으로 들어가지 못한다 — dev에서도 normal 53건이
-- 그 상태로 방치돼 있었고, prod로 넘어가면 34,532건 전부가 그렇게 된다.
--
-- ## 번호를 새로 매기지 않고 '밀어 올린다'
--
-- turn_seq는 9개 테이블이 참조한다(mem0_memory_registry·mem0_memory_sources·
-- mem0_ingest_candidates·relationship_events·shadow_prompt_traces·ai_usage_ledger·
-- chat_active_turns·conversation_focus, 그리고 memory_pipeline_states의 커서 4종).
-- 전부 재번호하면 이 참조를 같이 옮겨야 하고, 한 곳이라도 놓치면 기억이 엉뚱한 턴을 가리킨다.
--
-- 대신 **시간순을 지키면서 자리만 만든다**:
--
--   1. 사용자별로 좌표 없는 과거 턴 수 N을 센다
--   2. 이미 번호가 있는 턴을 전부 +N 한다 (참조 테이블도 같이)
--   3. 과거 턴에 1..N을 매긴다
--
-- 이미 번호가 있는 턴이 없으면 N만 매기고 끝이다 — **prod가 이 경우다**(컬럼 자체가 없으므로
-- 전부 NULL). 밀어 올리는 단계는 dev처럼 중간에 도입된 환경에서만 실제로 동작한다.
--
-- ## 한 턴의 정의
-- 사용자 발화 1 + 캐피 응답 1. `turn_position`은 1=user, 2=moly로 런타임과 같다.
-- 사용자 발화보다 먼저 온 캐피 메시지(선발화 잔재)는 턴 0이 되므로 제외한다 — 짝이 없는
-- 발화는 턴이 아니다. prod 실측으로 user 17,266 / moly 17,266으로 정확히 짝이 맞는다.
BEGIN;

-- 사용자별 과거 턴 수. 이후 단계가 전부 이 값을 쓴다.
CREATE TEMP TABLE _shift ON COMMIT DROP AS
SELECT user_id, count(*) FILTER (WHERE sender = 'user') AS n
FROM messages
WHERE kind = 'normal' AND turn_seq IS NULL
GROUP BY user_id
HAVING count(*) FILTER (WHERE sender = 'user') > 0;

-- 1) 기존 번호를 위로 민다.
--
--    `messages_user_turn_position_uq`(user_id, turn_seq, turn_position)는 DEFERRABLE이 아니라
--    UPDATE 도중 행 단위로 검사된다. 한 문장으로 +N 하면 아직 안 옮긴 행과 부딪힌다
--    (실측: turn 26을 쓰려는데 26이 아직 살아 있음). 그래서 **빈 구간을 경유해 두 번** 옮긴다.
UPDATE messages m
SET turn_seq = m.turn_seq + 1000000000
FROM _shift s
WHERE m.user_id = s.user_id AND m.turn_seq IS NOT NULL;

UPDATE messages m
SET turn_seq = m.turn_seq - 1000000000 + s.n
FROM _shift s
WHERE m.user_id = s.user_id AND m.turn_seq IS NOT NULL;

-- 같은 좌표를 저장하는 테이블들도 같은 폭으로 민다. 하나라도 빠지면 기억이 다른 턴을 가리킨다.
-- 이 중 여럿이 turn_seq를 포함한 UNIQUE를 갖고 있어(shadow_prompt_traces 등) 위와 같은 이유로
-- 전부 빈 구간을 경유한다.
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT * FROM (VALUES
      ('mem0_memory_registry',  'source_turn_seq'),
      ('mem0_memory_sources',   'source_turn_seq'),
      ('mem0_ingest_candidates','turn_seq'),
      ('relationship_events',   'turn_seq'),
      ('shadow_prompt_traces',  'turn_seq'),
      ('ai_usage_ledger',       'turn_seq'),
      ('chat_active_turns',     'turn_seq'),
      ('conversation_focus',    'expires_turn_seq'),
      ('chat_contexts',         'last_committed_turn_seq')
    ) AS v(tbl, col)
  LOOP
    EXECUTE format(
      'UPDATE %I t SET %I = t.%I + 1000000000 FROM _shift s
        WHERE t.user_id = s.user_id AND t.%I IS NOT NULL AND t.%I > 0',
      r.tbl, r.col, r.col, r.col, r.col);
    EXECUTE format(
      'UPDATE %I t SET %I = t.%I - 1000000000 + s.n FROM _shift s
        WHERE t.user_id = s.user_id AND t.%I > 1000000000',
      r.tbl, r.col, r.col, r.col);
  END LOOP;
END $$;

-- 파이프라인 커서. 0은 "아직 없음"이므로 밀지 않는다.
UPDATE memory_pipeline_states t
SET source_through_turn_seq = CASE WHEN t.source_through_turn_seq > 0
                                   THEN t.source_through_turn_seq + s.n ELSE 0 END,
    ingest_through_turn_seq = CASE WHEN t.ingest_through_turn_seq > 0
                                   THEN t.ingest_through_turn_seq + s.n ELSE 0 END,
    consolidated_through_turn_seq = CASE WHEN t.consolidated_through_turn_seq > 0
                                   THEN t.consolidated_through_turn_seq + s.n ELSE 0 END,
    historical_upper_turn_seq = CASE WHEN t.historical_upper_turn_seq > 0
                                   THEN t.historical_upper_turn_seq + s.n ELSE NULL END
FROM _shift s WHERE t.user_id = s.user_id;

-- 2) 과거 턴에 1..N을 매긴다. 사용자 발화가 턴을 열고, 뒤따르는 캐피 응답이 같은 턴을 닫는다.
WITH numbered AS (
  SELECT id,
         count(*) FILTER (WHERE sender = 'user') OVER (PARTITION BY user_id ORDER BY id) AS turn,
         sender
  FROM messages
  WHERE kind = 'normal' AND turn_seq IS NULL
)
UPDATE messages m
SET turn_seq = n.turn,
    turn_position = CASE WHEN n.sender = 'user' THEN 1 ELSE 2 END
FROM numbered n
WHERE m.id = n.id
  AND n.turn > 0;  -- 사용자 발화보다 앞선 캐피 메시지는 짝이 없다 — 턴이 아니다

COMMIT;
