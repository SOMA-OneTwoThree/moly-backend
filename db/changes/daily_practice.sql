-- Daily practice ("오늘의 작은 실천"): a shared pool of small actions and each user's daily offers.
-- Review/apply with db.apply; never apply schema.sql to an existing DB. Source of truth: db/schema.sql.
-- Apply before deploying the release: the old server ignores these tables. Additive only; no
-- existing rows change. Adding the foreign keys briefly locks profiles and routines (lock_timeout 2s).
-- Then load db/changes/daily_practice_seed.sql.
-- Rollback: deploy an image without the feature, then
-- DROP TABLE public.daily_practice_offers, public.daily_practice_actions;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
CREATE TABLE public.daily_practice_actions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  category text NOT NULL CHECK (category IN ('rest', 'body', 'calm', 'mind', 'self_kindness', 'connect', 'order')),
  slots text[] NOT NULL CHECK (cardinality(slots) >= 1 AND slots <@ ARRAY['morning', 'day', 'evening', 'night']),
  icon text NOT NULL CHECK (icon ~ '^[a-z0-9_]{1,64}$'),
  title_ko text NOT NULL UNIQUE CHECK (char_length(title_ko) BETWEEN 1 AND 18),
  title_en text NOT NULL CHECK (char_length(title_en) BETWEEN 1 AND 32),
  title_ja text NOT NULL CHECK (char_length(title_ja) BETWEEN 1 AND 18),
  speech_ko text NOT NULL CHECK (char_length(speech_ko) BETWEEN 1 AND 40),
  speech_en text NOT NULL CHECK (char_length(speech_en) BETWEEN 1 AND 100),
  speech_ja text NOT NULL CHECK (char_length(speech_ja) BETWEEN 1 AND 50),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
  source text NOT NULL CHECK (source IN ('seed', 'top_up')),
  prompt_version text NOT NULL,
  model text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
-- One row per offer. A skipped row stays as history and frees the day's single live row.
CREATE TABLE public.daily_practice_offers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  action_id uuid NOT NULL REFERENCES public.daily_practice_actions(id),
  local_date date NOT NULL,
  expires_at timestamptz NOT NULL,
  status text NOT NULL DEFAULT 'offered' CHECK (status IN ('offered', 'skipped', 'accepted', 'completed')),
  routine_id uuid REFERENCES public.routines(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, action_id, local_date),
  CONSTRAINT daily_practice_offers_routine_completed CHECK (routine_id IS NULL OR status = 'completed')
);
CREATE UNIQUE INDEX daily_practice_offers_one_live_uq ON public.daily_practice_offers USING btree (user_id, local_date) WHERE (status <> 'skipped');
CREATE INDEX daily_practice_offers_user_date_idx ON public.daily_practice_offers USING btree (user_id, local_date);
CREATE INDEX daily_practice_offers_routine_idx ON public.daily_practice_offers USING btree (routine_id) WHERE (routine_id IS NOT NULL);
ALTER TABLE public.daily_practice_actions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_practice_offers ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.daily_practice_actions, public.daily_practice_offers FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON public.daily_practice_actions, public.daily_practice_offers TO service_role;
