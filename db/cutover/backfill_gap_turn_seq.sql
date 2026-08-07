-- 빈 구간 메시지에 턴 좌표를 매긴다 — 배포 후 3-2-3.
--
-- 무엇을 고치나
--   1단계(마이그레이션) 이후 배포 전까지 구 코드가 만든 대화 메시지는 `turn_seq`가 비어 있다.
--   구 코드는 이 컬럼을 모르기 때문이다. 기억 파이프라인의 모든 조회가
--   `turn_seq IS NOT NULL`로 거르므로(`app/services/memory_pipeline.py` 181행),
--   좌표 없는 메시지는 **영원히 장기기억으로 들어가지 못한다.**
--
-- ⚠️ **`20260806_backfill_turn_seq.sql`을 다시 돌리면 안 된다.**
--   그 파일은 좌표 없는 메시지를 **과거 대화로 보고** 1번부터 번호를 매기면서 기존 번호를
--   전부 위로 밀어 올린다. 빈 구간 메시지는 가장 **최근** 대화인데 가장 앞 번호를 받게 되어
--   시간 순서가 뒤집힌다. 참조하는 표 9개와 파이프라인 커서까지 같이 밀리므로 되돌리기도 어렵다.
--
-- 이 파일은 반대로 한다 — **기존 최대 번호 뒤에 이어 붙인다.**
--   기존 번호를 건드리지 않으므로 참조 표도, 커서도 그대로 둔다. 새로 붙는 번호가 커서보다
--   크므로 파이프라인의 `MIN(turn_seq) > cursor` 조회가 자연스럽게 집어간다.
--
-- 한 턴의 정의
--   사용자 발화 1 + 캐피 응답 1. `turn_position`은 1=user, 2=moly로 런타임과 같다.
--   사용자 발화보다 먼저 온 캐피 메시지는 짝이 없으므로 턴이 아니다(번호를 주지 않는다).
--   메시지 `id`는 bigint 연속번호라 삽입 순서와 같다 — 그래서 정렬 기준으로 쓴다.
--
-- 적용:
--   PYTHONPATH=. uv run python db/apply.py db/cutover/backfill_gap_turn_seq.sql --env prod
--   PYTHONPATH=. uv run python db/apply.py db/cutover/backfill_gap_turn_seq.sql --env prod --allow-prod --commit

BEGIN;

WITH mx AS (
  -- 사용자별 현재 최대 턴 번호. 없으면 0에서 시작한다.
  SELECT user_id, max(turn_seq) AS m
  FROM public.messages
  WHERE kind = 'normal' AND turn_seq IS NOT NULL
  GROUP BY user_id
),
numbered AS (
  -- 좌표 없는 메시지에 사용자 발화 기준으로 1..N을 매긴다.
  -- 캐피 응답은 바로 앞 사용자 발화와 같은 번호를 받는다(누적 개수가 같으므로).
  SELECT id, user_id, sender,
         count(*) FILTER (WHERE sender = 'user')
           OVER (PARTITION BY user_id ORDER BY id) AS n
  FROM public.messages
  WHERE kind = 'normal' AND turn_seq IS NULL
)
UPDATE public.messages m
SET turn_seq = COALESCE(x.m, 0) + n.n,
    turn_position = CASE WHEN n.sender = 'user' THEN 1 ELSE 2 END
FROM numbered n
LEFT JOIN mx x ON x.user_id = n.user_id
WHERE m.id = n.id
  AND n.n > 0;   -- 첫 사용자 발화 앞의 캐피 메시지는 턴이 아니다

COMMIT;

-- 적용 뒤 확인
--   SELECT count(*) FROM messages WHERE kind='normal' AND turn_seq IS NULL;
--     -> 0 이어야 한다(첫 발화 앞 캐피 메시지가 있으면 그만큼 남는다).
--   SELECT count(*) FROM (
--     SELECT user_id, turn_seq, turn_position, count(*) c FROM messages
--     WHERE kind='normal' AND turn_seq IS NOT NULL
--     GROUP BY 1,2,3 HAVING count(*) > 1) t;
--     -> 0 이어야 한다(같은 좌표가 겹치지 않는다).
