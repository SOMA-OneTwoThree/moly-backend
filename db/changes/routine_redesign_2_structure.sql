-- Routine redesign 2/3: routine columns and the new tables, without data. Run after 1/3, before 3/3.
-- Target: public.routines gets icon, color (constant defaults), template_id, deleted_on (nullable);
-- new tables routine_schedules, routine_skips, routine_template_categories, routine_templates with
-- RLS on, no policy and service_role-only grants. No row is rewritten.
-- Before (read-only): SELECT count(*) FROM public.routines;  -- prod 2026-10-08: 3,376; no row is written
-- Lock: the first statement takes ACCESS EXCLUSIVE on routines and keeps it until COMMIT (a few ms);
-- the routines foreign keys of the new tables need no stronger lock afterwards. It waits up to 2s for
-- running transactions and routine requests queue behind it meanwhile. No other existing table is
-- locked, so it cannot deadlock with requests. On lock timeout it rolls back unchanged; retry later.
-- A second run fails on the existing columns and changes nothing.
-- Mixed versions: the previous image and moly-auth never name the new columns or tables; constraints
-- exist only on the new tables, so the previous image's preflight passes. The new image's preflight
-- also passes from here, but do not merge or deploy before 3/3: until then the template catalogue is
-- empty and the default routines lack their template icon and template_id (records are still right
-- through the code fallbacks).
-- Rollback (previous image deployed first; loses weekday history, skips and icons):
--   DROP TABLE public.routine_schedules, public.routine_skips, public.routine_templates,
--     public.routine_template_categories;
--   ALTER TABLE public.routines DROP COLUMN icon, DROP COLUMN color, DROP COLUMN template_id,
--     DROP COLUMN deleted_on;
-- BEGIN/COMMIT keep SET LOCAL effective under plain psql; db.apply strips them and uses its own transaction.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE public.routines
  ADD COLUMN icon text NOT NULL DEFAULT 'seedling',
  ADD COLUMN color text NOT NULL DEFAULT 'peach',
  ADD COLUMN template_id text,
  ADD COLUMN deleted_on date;

CREATE TABLE public.routine_schedules (
  routine_id uuid NOT NULL,
  user_id uuid NOT NULL,
  effective_from date NOT NULL,
  days_of_week smallint[] NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT routine_schedules_pkey PRIMARY KEY (routine_id, effective_from),
  CONSTRAINT routine_schedules_user_routine_fk FOREIGN KEY (user_id, routine_id)
    REFERENCES public.routines(user_id, id) ON DELETE CASCADE
);
CREATE TABLE public.routine_skips (
  routine_id uuid NOT NULL,
  user_id uuid NOT NULL,
  activity_date date NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT routine_skips_pkey PRIMARY KEY (routine_id, activity_date),
  CONSTRAINT routine_skips_user_routine_fk FOREIGN KEY (user_id, routine_id)
    REFERENCES public.routines(user_id, id) ON DELETE CASCADE
);
CREATE INDEX routine_skips_user_date_idx ON public.routine_skips USING btree (user_id, activity_date);
CREATE TABLE public.routine_template_categories (
  id text PRIMARY KEY CHECK (id ~ '^[a-z0-9_]{1,64}$'),
  name_i18n jsonb NOT NULL CHECK (COALESCE(jsonb_typeof(name_i18n -> 'ko'), '') = 'string'),
  sort_order smallint NOT NULL DEFAULT 0,
  is_active boolean NOT NULL DEFAULT true
);
CREATE TABLE public.routine_templates (
  id text PRIMARY KEY CHECK (id ~ '^[a-z0-9_]{1,64}$'),
  category_id text NOT NULL,
  name_i18n jsonb NOT NULL CHECK (COALESCE(jsonb_typeof(name_i18n -> 'ko'), '') = 'string'),
  icon text NOT NULL CHECK (icon ~ '^[a-z0-9_]{1,64}$'),
  color text NOT NULL CHECK (color IN ('pink', 'peach', 'yellow', 'green', 'blue', 'mint', 'lavender')),
  days_of_week smallint[] NOT NULL
    CHECK (cardinality(days_of_week) BETWEEN 1 AND 7 AND days_of_week <@ '{1,2,3,4,5,6,7}'::smallint[]),
  is_recommended boolean NOT NULL DEFAULT false,
  sort_order smallint NOT NULL DEFAULT 0,
  is_active boolean NOT NULL DEFAULT true,
  CONSTRAINT routine_templates_category_fk FOREIGN KEY (category_id)
    REFERENCES public.routine_template_categories(id)
);
ALTER TABLE public.routine_schedules ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.routine_skips ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.routine_template_categories ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.routine_templates ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.routine_schedules, public.routine_skips,
  public.routine_template_categories, public.routine_templates
  FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON public.routine_schedules, public.routine_skips,
  public.routine_template_categories, public.routine_templates
  TO service_role;
COMMIT;
