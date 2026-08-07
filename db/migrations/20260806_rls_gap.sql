-- 적용: PYTHONPATH=. uv run python db/apply.py db/migrations/20260806_rls_gap.sql --commit
--
-- 신규 테이블 2개가 클라이언트에 열려 있었다.
--
-- public 53개 중 `shadow_prompt_traces`·`user_schedules`만 RLS가 꺼져 있었고, 게다가 두
-- 테이블 모두 `anon`·`authenticated`에 SELECT·INSERT·UPDATE·DELETE·**TRUNCATE**까지 부여돼
-- 있었다(실측). 이 프로젝트는 정책(policy)을 하나도 두지 않고 "RLS 켜짐 + 정책 0 = 전면 차단",
-- 서버는 service_role로 우회하는 방식이다. 그래서 RLS가 꺼진 표는 **아무 방어가 없다**.
--
-- 앱에 내장되는 공개 anon 키만으로 PostgREST를 통해 직접 읽고 쓰고 비울 수 있었다는 뜻이다.
-- `user_schedules.next_due_at`을 건드리면 일기 발행과 저녁 푸시 스케줄이 망가진다.
-- "클라이언트 DB 직접 쓰기 금지"가 이 레포의 기본 원칙인데 그 통로가 열려 있었다.
--
-- 원인은 이 두 테이블의 생성 마이그레이션이 다른 표와 달리 ENABLE RLS/REVOKE를 빠뜨린 것이다.
BEGIN;

ALTER TABLE public.shadow_prompt_traces ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_schedules       ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.shadow_prompt_traces FROM anon, authenticated;
REVOKE ALL ON public.user_schedules       FROM anon, authenticated;

COMMIT;
