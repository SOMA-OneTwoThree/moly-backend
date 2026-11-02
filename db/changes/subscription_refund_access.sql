-- Additive only. Apply to the reviewed development project first.
-- Runtime adoption remains OFF until all API and worker instances are compatible.
ALTER TABLE public.subscriptions ADD COLUMN IF NOT EXISTS access_evidence jsonb;
ALTER TABLE public.revenuecat_events ADD COLUMN IF NOT EXISTS processing_state jsonb;
