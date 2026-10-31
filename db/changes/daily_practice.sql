-- Daily practice ("오늘의 작은 실천"): hand-written action templates and each user's daily options.
-- Review/apply with db.apply; never apply schema.sql to an existing DB. Source of truth: db/schema.sql.
-- Apply before deploying the release: the old server ignores these tables. Additive only; no
-- existing rows change. Adding the foreign keys briefly locks profiles and routines (lock_timeout 2s).
-- Ships no templates; see docs/OPERATIONS.md for how to add them.
-- Rollback: deploy an image without the feature, then
-- DROP TABLE public.daily_practice_offers, public.daily_practice_templates;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
CREATE TABLE public.daily_practice_templates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title_ko text NOT NULL UNIQUE CHECK (char_length(title_ko) BETWEEN 1 AND 60),
  title_en text NOT NULL CHECK (char_length(title_en) BETWEEN 1 AND 60),
  title_ja text NOT NULL CHECK (char_length(title_ja) BETWEEN 1 AND 60),
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);
-- One row per option shown. Unchosen rows stay as history for the 30-day avoidance.
CREATE TABLE public.daily_practice_offers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  template_id uuid NOT NULL REFERENCES public.daily_practice_templates(id),
  local_date date NOT NULL,
  status text NOT NULL DEFAULT 'offered' CHECK (status IN ('offered', 'accepted', 'completed')),
  options_expire_at timestamptz NOT NULL,
  deadline timestamptz,
  completed_local_date date,
  routine_id uuid REFERENCES public.routines(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, template_id, local_date),
  CONSTRAINT daily_practice_offers_routine_completed CHECK (routine_id IS NULL OR status = 'completed')
);
CREATE UNIQUE INDEX daily_practice_offers_one_choice_uq ON public.daily_practice_offers USING btree (user_id, local_date) WHERE (status <> 'offered');
CREATE INDEX daily_practice_offers_user_date_idx ON public.daily_practice_offers USING btree (user_id, local_date);
CREATE INDEX daily_practice_offers_routine_idx ON public.daily_practice_offers USING btree (routine_id) WHERE (routine_id IS NOT NULL);
ALTER TABLE public.daily_practice_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_practice_offers ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.daily_practice_templates, public.daily_practice_offers FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON public.daily_practice_templates, public.daily_practice_offers TO service_role;
