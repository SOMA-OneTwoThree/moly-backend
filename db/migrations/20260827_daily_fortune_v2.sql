-- DEV-FIRST: applied v1 fortune tables are migrated in place without losing today's unlock.
-- The already-applied 20260827_daily_fortune.sql must remain immutable for ledger reproducibility.
BEGIN;

ALTER TABLE public.fortune_profiles
  DROP CONSTRAINT IF EXISTS fortune_profiles_precision_ck,
  DROP CONSTRAINT IF EXISTS fortune_profiles_gender_cohort_check,
  DROP CONSTRAINT IF EXISTS fortune_profiles_gender_check;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='public' AND table_name='fortune_profiles' AND column_name='gender_cohort'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='public' AND table_name='fortune_profiles' AND column_name='gender'
  ) THEN
    ALTER TABLE public.fortune_profiles RENAME COLUMN gender_cohort TO gender;
  END IF;
END $$;

ALTER TABLE public.fortune_profiles
  ADD COLUMN IF NOT EXISTS gender text,
  DROP COLUMN IF EXISTS birth_time,
  DROP COLUMN IF EXISTS birth_timezone,
  DROP COLUMN IF EXISTS birth_utc_offset_minutes,
  DROP COLUMN IF EXISTS time_source,
  DROP COLUMN IF EXISTS precision,
  DROP COLUMN IF EXISTS natal_positions,
  DROP COLUMN IF EXISTS ephemeris_version;

ALTER TABLE public.fortune_profiles ALTER COLUMN gender SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='fortune_profiles_gender_ck'
      AND conrelid='public.fortune_profiles'::regclass
  ) THEN
    ALTER TABLE public.fortune_profiles ADD CONSTRAINT fortune_profiles_gender_ck
      CHECK (gender IN ('man','woman','undisclosed'));
  END IF;
END $$;

-- Run the one-time invalidation only before this migration is recorded in schema_migrations.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM public.schema_migrations
    WHERE migration_name='20260827_daily_fortune_v2.sql'
  ) THEN
    UPDATE public.fortune_profiles SET revision=revision+1, updated_at=now();
  END IF;
END $$;

ALTER TABLE public.daily_fortunes
  ADD COLUMN IF NOT EXISTS result_schema_version integer;
UPDATE public.daily_fortunes
SET result_schema_version=2
WHERE result_schema_version IS NULL;
ALTER TABLE public.daily_fortunes
  ALTER COLUMN result_schema_version SET DEFAULT 3,
  ALTER COLUMN result_schema_version SET NOT NULL,
  DROP CONSTRAINT IF EXISTS daily_fortunes_result_schema_version_ck,
  DROP CONSTRAINT IF EXISTS daily_fortunes_unlock_ck;
ALTER TABLE public.daily_fortunes ADD CONSTRAINT daily_fortunes_result_schema_version_ck
  CHECK (result_schema_version >= 2);
ALTER TABLE public.daily_fortunes ADD CONSTRAINT daily_fortunes_unlock_ck CHECK (
  (unlock_state='locked' AND unlock_source IS NULL AND unlocked_at IS NULL AND revealed_at IS NULL)
  OR
  (unlock_state='unlocked' AND unlock_source IS NOT NULL AND unlocked_at IS NOT NULL)
);

DROP INDEX IF EXISTS public.fortune_ad_sessions_retention_idx;
CREATE INDEX IF NOT EXISTS fortune_ad_sessions_retention_idx
  ON public.fortune_ad_sessions(expires_at,session_id);

COMMIT;
