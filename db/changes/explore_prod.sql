-- PRODUCTION ONLY. Review/apply using db.apply; never apply schema.sql to an existing DB.
-- The explore training/article tables that dev received as explore_training.sql,
-- training_submissions.sql, training_claim_reasons.sql and explore_articles.sql, in that order and
-- unchanged except that their hay_transactions_type_check statements are left out: those carried
-- dev's type list without main's mood_reward. hay_transactions_types.sql sets the list afterwards.
-- Additive: new tables only, plus one CHECK widened on the new training_claims table.
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';

-- ===== explore_training.sql
-- Review/apply using db.apply; never apply schema.sql to an existing DB.
-- Apply before deploying the training release: the old server ignores these tables, and the
-- new server writes training_reward ledger rows. Additive only; no member rows are changed.

-- Explore trainings. Stage content lives in app/resources/trainings; these rows are per account.
-- Only passed answers are kept. last_result replays the latest submission for its request id.
CREATE TABLE public.training_progress (
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  training_id text NOT NULL,
  next_stage smallint CHECK (next_stage BETWEEN 1 AND 30),
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  last_request_id uuid,
  last_result jsonb CHECK (jsonb_typeof(last_result) = 'object'),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, training_id),
  CONSTRAINT training_progress_completion CHECK ((next_stage IS NULL) = (completed_at IS NOT NULL)),
  CONSTRAINT training_progress_last_result CHECK ((last_request_id IS NULL) = (last_result IS NULL))
);
CREATE TABLE public.training_answers (
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  training_id text NOT NULL,
  stage smallint NOT NULL CHECK (stage BETWEEN 1 AND 30),
  option_id text,
  answer_text text CHECK (char_length(answer_text) BETWEEN 1 AND 200),
  passed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, training_id, stage),
  CONSTRAINT training_answers_one_kind CHECK ((option_id IS NULL) <> (answer_text IS NULL))
);
-- Kept on restart so each review point pays once per account.
CREATE TABLE public.training_rewards (
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  training_id text NOT NULL,
  stage smallint NOT NULL CHECK (stage BETWEEN 1 AND 30),
  amount integer NOT NULL CHECK (amount > 0),
  granted_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, training_id, stage)
);
-- No row means a full set of five tries.
CREATE TABLE public.training_tries (
  user_id uuid PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  tries_left smallint NOT NULL CHECK (tries_left BETWEEN 0 AND 5),
  refill_from timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT training_tries_refill CHECK ((tries_left = 5) = (refill_from IS NULL))
);
-- Written only when a user disputes an AI grade; the grade itself never changes.
CREATE TABLE public.training_claims (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  training_id text NOT NULL,
  stage smallint NOT NULL CHECK (stage BETWEEN 1 AND 30),
  reason text NOT NULL CHECK (reason IN ('should_pass', 'should_fail', 'wrong_hint')),
  answer_text text NOT NULL CHECK (char_length(answer_text) BETWEEN 1 AND 200),
  verdict text NOT NULL CHECK (verdict IN ('passed', 'retry')),
  hint text CHECK (char_length(hint) BETWEEN 1 AND 300),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX training_claims_user_idx ON public.training_claims USING btree (user_id);
ALTER TABLE public.training_progress ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.training_answers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.training_rewards ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.training_tries ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.training_claims ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.training_progress, public.training_answers, public.training_rewards,
  public.training_tries, public.training_claims
  FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON public.training_progress, public.training_answers, public.training_rewards,
  public.training_tries, public.training_claims
  TO service_role;

-- ===== training_submissions.sql
-- Apply after explore_training.sql and before deploying request receipt support.
-- Additive: no existing answers/progress are changed. No backfill of rejected text.
CREATE TABLE public.training_submissions (
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  training_id text NOT NULL,
  request_id uuid NOT NULL,
  stage smallint NOT NULL CHECK (stage BETWEEN 1 AND 30),
  payload_hash text NOT NULL CHECK (length(payload_hash) = 64),
  lease_token uuid NOT NULL,
  lease_until timestamptz NOT NULL,
  result jsonb CHECK (jsonb_typeof(result) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, training_id, request_id)
);
ALTER TABLE public.training_submissions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.training_submissions FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON public.training_submissions TO service_role;

-- ===== training_claim_reasons.sql
-- Expand feedback categories; existing values and rows remain valid.
ALTER TABLE public.training_claims DROP CONSTRAINT training_claims_reason_check;
ALTER TABLE public.training_claims ADD CONSTRAINT training_claims_reason_check
  CHECK (reason IN ('should_pass', 'should_fail', 'wrong_hint', 'technical_error', 'other'));

-- ===== explore_articles.sql
CREATE TABLE IF NOT EXISTS public.article_progress (
    user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    article_id text NOT NULL CHECK (article_id IN ('sample-1','sample-2','sample-3','sample-4','sample-5','sample-6','sample-7')),
    feedback text CHECK (feedback IN ('helpful','not_helpful')),
    read_at timestamptz NOT NULL DEFAULT now(),
    rewarded_at timestamptz,
    PRIMARY KEY (user_id, article_id)
);
ALTER TABLE public.article_progress DROP CONSTRAINT article_progress_article_id_check;
ALTER TABLE public.article_progress ADD CONSTRAINT article_progress_article_id_check
CHECK (article_id IN ('sample-1','sample-2','sample-3','sample-4','sample-5','sample-6','sample-7'));
ALTER TABLE public.article_progress ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.article_progress FROM anon, authenticated;
GRANT ALL ON public.article_progress TO service_role;
