-- Marks Old Radio (fading-static) as a subscriber-only BGM track.
-- Apply before deploying the app server that reads bgm_tracks.is_subscriber_only.
-- The previous server selects named columns, so it ignores the new column.
-- Expected: column added (metadata-only, constant default), 1 row updated.
-- Rollback: UPDATE public.bgm_tracks SET is_subscriber_only = false; the column may stay.
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE public.bgm_tracks
  ADD COLUMN is_subscriber_only boolean NOT NULL DEFAULT false;

UPDATE public.bgm_tracks
SET is_subscriber_only = true
WHERE id = 'fading-static';
