-- Release preparation: 날짜별 오늘의 글귀 테이블. 기존 dev/prod DB에는 없는 테이블이다.
-- Source of truth remains db/schema.sql; 새 테이블 추가라 기존 배포와 혼합 실행해도 호환된다.
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
CREATE TABLE public.daily_affirmations (
    local_date date NOT NULL,
    text_ko text NOT NULL,
    text_en text NOT NULL,
    text_ja text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE ONLY public.daily_affirmations
    ADD CONSTRAINT daily_affirmations_pkey PRIMARY KEY (local_date);
ALTER TABLE public.daily_affirmations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.daily_affirmations FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.daily_affirmations TO service_role;
