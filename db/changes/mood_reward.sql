-- 감정일기 기록 보상 — user_daily_stats.mood_reward_claimed_at + 원장 type mood_reward. apply with db.apply.
-- Apply BEFORE the backend image that serves /charging-station/mood-reward: the regenerated schema
-- contract expects both changes, and the new code reads the column and writes the type. Old images
-- never name the column (nullable, no default) and never write mood_reward, so a mixed-version window
-- is safe. No backfill: NULL = not claimed.
-- Expected: brief ACCESS EXCLUSIVE locks on user_daily_stats (catalog-only column add) and
-- hay_transactions (CHECK revalidation scans ~10k rows); on lock timeout roll back and retry at a quiet time.
-- Rollback: deploy an image without the mood reward first, delete or retype any mood_reward rows, then
-- ALTER TABLE public.user_daily_stats DROP COLUMN mood_reward_claimed_at; and restore the previous CHECK.
-- BEGIN/COMMIT keep SET LOCAL effective under plain psql; db.apply strips them and uses its own transaction.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
ALTER TABLE public.user_daily_stats ADD COLUMN mood_reward_claimed_at timestamp with time zone;
ALTER TABLE public.hay_transactions DROP CONSTRAINT hay_transactions_type_check;
ALTER TABLE public.hay_transactions ADD CONSTRAINT hay_transactions_type_check CHECK ((type = ANY (ARRAY['attendance'::text, 'ad_reward'::text, 'routine_reward'::text, 'mood_reward'::text, 'iap_purchase'::text, 'subscription_grant'::text, 'shop_purchase'::text, 'refund_revoke'::text, 'admin_adjustment'::text])));
COMMIT;
-- Validate with the regenerated schema contract (uv run python -m db.verify --env <env>).
