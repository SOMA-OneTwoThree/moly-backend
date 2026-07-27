-- 적용: python db/apply.py db/migrations/20260727_diary_gen_claims.sql --commit
-- 일기 생성 클레임(SOMA-373) — 워커 틱 중첩 시 같은 (유저,날짜) 일기를 두 프로세스가 동시에
-- LLM 생성하지 않도록 상호배제. 세션 advisory lock은 커넥션 풀 반환·pgbouncer 트랜잭션 풀링과
-- 맞지 않아(내부 커밋 시 락이 다른 커넥션으로 새거나 미지원) 커밋된 행 기반 클레임을 쓴다.
-- claimed_at 만료(30분)로 크래시된 클레임은 다음 틱이 회수한다. 정상 종료 시 삭제(누적 방지).
BEGIN;

CREATE TABLE IF NOT EXISTS public.diary_gen_claims (
  user_id     uuid        NOT NULL,
  target_date date        NOT NULL,
  claimed_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, target_date)
);

-- RLS deny-default(다른 테이블과 동일 불변식) — 서버는 owner 롤이라 우회, 클라(anon/authenticated)
-- 직접 접근 차단. 없으면 악의적 클라가 임의 유저 claim을 INSERT해 워커 일기 생성을 스킵시킬 수 있다.
ALTER TABLE public.diary_gen_claims ENABLE ROW LEVEL SECURITY;

COMMIT;
