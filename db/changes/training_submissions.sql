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
