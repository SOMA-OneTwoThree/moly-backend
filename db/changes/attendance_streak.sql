-- attendance_streak_seeds — 연속 출석이 이어받은 루틴 연속(유저당 1행). apply with db.apply.
-- Apply BEFORE the backend image that serves /attendance: the new code reads and writes this table.
-- Old images never name it, so a mixed-version window is safe. No backfill: rows are created lazily
-- at each account's first check-in.
-- Expected: new empty table, plus a brief SHARE ROW EXCLUSIVE lock on profiles for the FK.
-- On lock timeout roll back and retry at a quiet time.
-- Rollback: DROP TABLE public.attendance_streak_seeds; (deploy an image without /attendance first).
-- BEGIN/COMMIT keep SET LOCAL effective under plain psql; db.apply strips them and uses its own transaction.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
CREATE TABLE public.attendance_streak_seeds (
    user_id uuid NOT NULL,
    through_date date NOT NULL,
    streak integer NOT NULL,
    best_streak integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT attendance_streak_seeds_best_streak_check CHECK ((best_streak >= 0)),
    CONSTRAINT attendance_streak_seeds_streak_check CHECK ((streak >= 0))
);
ALTER TABLE ONLY public.attendance_streak_seeds
    ADD CONSTRAINT attendance_streak_seeds_pkey PRIMARY KEY (user_id);
ALTER TABLE ONLY public.attendance_streak_seeds
    ADD CONSTRAINT attendance_streak_seeds_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;
ALTER TABLE public.attendance_streak_seeds ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.attendance_streak_seeds FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON TABLE public.attendance_streak_seeds TO service_role;
COMMIT;
-- Validate with the regenerated schema contract (uv run python -m db.verify --env <env>).
