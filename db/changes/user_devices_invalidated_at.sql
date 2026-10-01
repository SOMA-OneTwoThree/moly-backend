-- user_devices.invalidated_at — FCM이 무효 확정한 푸시 토큰의 비활성 표시 (release handoff, apply with db.apply).
-- Apply BEFORE the new worker image: the regenerated schema contract expects the column, and the
-- new code reads it. Old images and moly-auth never name this column (nullable, no default), so a
-- mixed-version window is safe. No backfill: NULL = valid token (today's behaviour).
-- Expected: a brief ACCESS EXCLUSIVE lock (nullable column without default = catalog-only, no table rewrite);
-- on lock timeout roll back and retry at a quiet time.
-- Rollback: ALTER TABLE public.user_devices DROP COLUMN invalidated_at; (deploy an image without the column first).
-- BEGIN/COMMIT keep SET LOCAL effective under plain psql; db.apply strips them and uses its own transaction.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
ALTER TABLE public.user_devices ADD COLUMN invalidated_at timestamp with time zone;
COMMIT;
-- Validate with the regenerated schema contract (uv run python -m db.verify --env <env>).
