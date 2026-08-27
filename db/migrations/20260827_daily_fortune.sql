-- Additive dev-first schema for the daily fortune MVP. No backfill and no runtime DDL.
BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS public.fortune_profiles (
  user_id uuid PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  gender_cohort text NOT NULL CHECK (gender_cohort IN ('man','woman','undisclosed')),
  birth_date date NOT NULL,
  birth_time time NOT NULL,
  birth_timezone text NOT NULL,
  birth_utc_offset_minutes integer NOT NULL CHECK (birth_utc_offset_minutes BETWEEN -840 AND 840),
  time_source text NOT NULL CHECK (time_source IN ('actual','assumed_noon')),
  precision text NOT NULL CHECK (precision IN ('basic','timed')),
  natal_positions jsonb NOT NULL CHECK (jsonb_typeof(natal_positions)='object'),
  revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
  ephemeris_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT fortune_profiles_precision_ck CHECK (
    (time_source='actual' AND precision='timed')
    OR (time_source='assumed_noon' AND precision='basic')
  )
);

CREATE TABLE IF NOT EXISTS public.daily_fortunes (
  user_id uuid PRIMARY KEY REFERENCES public.fortune_profiles(user_id) ON DELETE CASCADE,
  fortune_date date NOT NULL,
  timezone_snapshot text NOT NULL,
  profile_revision bigint NOT NULL CHECK (profile_revision >= 1),
  semantic_result jsonb NOT NULL CHECK (jsonb_typeof(semantic_result)='object'),
  copy_by_locale jsonb NOT NULL CHECK (jsonb_typeof(copy_by_locale)='object'),
  unlock_state text NOT NULL CHECK (unlock_state IN ('locked','unlocked')),
  unlock_source text CHECK (unlock_source IN ('subscription','trial','rewarded_ad')),
  unlocked_at timestamptz,
  revealed_at timestamptz,
  ephemeris_version text NOT NULL,
  rule_version text NOT NULL,
  copy_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT daily_fortunes_unlock_ck CHECK (
    (unlock_state='locked' AND unlock_source IS NULL AND unlocked_at IS NULL AND revealed_at IS NULL)
    OR
    (unlock_state='unlocked' AND unlock_source IS NOT NULL AND unlocked_at IS NOT NULL
      AND revealed_at IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS public.fortune_ad_sessions (
  session_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.fortune_profiles(user_id) ON DELETE CASCADE,
  fortune_date date NOT NULL,
  client_request_id uuid NOT NULL,
  verified boolean NOT NULL DEFAULT false,
  ssv_transaction_id text UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  verified_at timestamptz,
  CONSTRAINT fortune_ad_sessions_client_uq UNIQUE(user_id,client_request_id),
  CONSTRAINT fortune_ad_sessions_expiry_ck CHECK (expires_at > created_at),
  CONSTRAINT fortune_ad_sessions_verified_ck CHECK (
    (verified=false AND ssv_transaction_id IS NULL AND verified_at IS NULL)
    OR
    (verified=true AND ssv_transaction_id IS NOT NULL AND verified_at IS NOT NULL)
  )
);
CREATE INDEX IF NOT EXISTS fortune_ad_sessions_lookup_idx
  ON public.fortune_ad_sessions(user_id,fortune_date,verified);
CREATE INDEX IF NOT EXISTS fortune_ad_sessions_retention_idx
  ON public.fortune_ad_sessions(created_at,session_id);

ALTER TABLE public.fortune_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_fortunes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.fortune_ad_sessions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.fortune_profiles FROM anon, authenticated;
REVOKE ALL ON public.daily_fortunes FROM anon, authenticated;
REVOKE ALL ON public.fortune_ad_sessions FROM anon, authenticated;

COMMIT;
