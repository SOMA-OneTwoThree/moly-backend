-- 적용: python db/apply.py db/migrations/20260807_backfill_last_active.sql --commit
-- chat_contexts.last_active_at 백필 — 저녁 푸시 다양화(미접속 사다리)의 전제.
--
-- 컬럼은 20260804_chat_last_active.sql이 만들었지만 쓰기 경로(_touch_last_active)는
-- 기억 v2 코드에만 있다. prod 실측(2026-08-07): 525행 전원 NULL. 백필 없이 다양화가
-- 배포되면 어제 대화한 유저까지 전원 "오랜만이야"(default_long)로 오분류되고, 오류도
-- 로그도 없이 조용히 틀린다.
--
-- 백필 값 = 그 유저의 마지막 커밋된 발화(messages, sender='user', kind='normal').
-- last_active_at의 정의("마지막 커밋된 대화 턴")와 일치한다. greeting은 유저 발화가 아니다.
--
-- UPDATE가 아니라 INSERT ... ON CONFLICT다 — 대화는 있는데 chat_contexts 행이 없는
-- 유저(prod 실측 존재)는 UPDATE-only로는 영영 미백필로 남는다. 최소 INSERT는
-- _touch_last_active(chat.py)가 프로덕션에서 이미 하는 것과 같은 형태라 server_default·
-- 트리거 모두 안전이 증명돼 있다. 이미 값이 있는 행은 건드리지 않는다(멱등 — v2 코드가
-- 쓰기 시작한 뒤 재실행해도 안전).
BEGIN;

INSERT INTO public.chat_contexts (user_id, last_active_at)
SELECT user_id, max(created_at)
FROM public.messages
WHERE sender = 'user' AND kind = 'normal'
GROUP BY user_id
HAVING max(created_at) IS NOT NULL
ON CONFLICT (user_id) DO UPDATE
  SET last_active_at = EXCLUDED.last_active_at
  WHERE chat_contexts.last_active_at IS NULL;

COMMIT;
