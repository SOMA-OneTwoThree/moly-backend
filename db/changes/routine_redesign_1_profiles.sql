-- Routine redesign 1/3: profiles.routine_template_selection_at. Run 1 -> 2 -> 3, each file as its own
-- transaction (never concatenated), each reviewed with db.apply (dry run, then --commit
-- --expected-sha256), dev before prod, at a quiet time.
-- Target: public.profiles gets one nullable column without a default (catalog-only, no row rewrite).
-- Before (read-only): SELECT count(*) FROM public.profiles;  -- prod 2026-10-08: 1,330; no row is written
-- Lock: ACCESS EXCLUSIVE on profiles for a few ms. It waits up to lock_timeout (2s) for running
-- transactions and profile requests queue behind it meanwhile. This file takes no other lock, so it
-- cannot deadlock with requests. On lock timeout it rolls back unchanged; retry later. A second run
-- fails on the existing column and changes nothing.
-- Mixed versions: the previous image and moly-auth never name the column; the previous image's
-- preflight accepts an additive nullable column.
-- Rollback (only while no deployed image reads the column):
--   ALTER TABLE public.profiles DROP COLUMN routine_template_selection_at;
-- BEGIN/COMMIT keep SET LOCAL effective under plain psql; db.apply strips them and uses its own transaction.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE public.profiles ADD COLUMN routine_template_selection_at timestamp with time zone;
COMMIT;
