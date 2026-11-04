-- Metadata only. Existing rows stay NULL and all original content stays intact.
-- Apply before the compatible API/worker release; no rewrite/backfill/model call.
SET LOCAL lock_timeout = '100ms';
SET LOCAL statement_timeout = '750ms';
LOCK TABLE public.chat_contexts, public.user_interaction_contracts IN ACCESS EXCLUSIVE MODE NOWAIT;
ALTER TABLE public.chat_contexts ADD COLUMN IF NOT EXISTS retention_compat_json jsonb;
ALTER TABLE public.chat_contexts ADD COLUMN IF NOT EXISTS retention_compat_summary_invalidated_revision bigint;
ALTER TABLE public.user_interaction_contracts ADD COLUMN IF NOT EXISTS validation_json jsonb;
