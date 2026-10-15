-- Release preparation: existing dev topic schema, absent from production on 2026-09-09.
-- Apply with db.apply only after inspecting the target schema and reviewing the hash.
-- Source of truth remains db/schema.sql; no application tables are added by morning push itself.
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
-- Topic offers and prepared conversation openings.
CREATE TABLE IF NOT EXISTS public.user_topic_states (
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  placement text NOT NULL CHECK (placement = 'home_blind'),
  offer_id uuid NOT NULL UNIQUE,
  offer_sequence bigint NOT NULL CHECK (offer_sequence > 0),
  topic_id text NOT NULL,
  topic_revision text NOT NULL,
  questions jsonb NOT NULL,
  day_high_watermark date NOT NULL,
  completed boolean NOT NULL DEFAULT false,
  daily_open_count smallint NOT NULL DEFAULT 0 CONSTRAINT topic_daily_open_limit CHECK (daily_open_count BETWEEN 0 AND 2),
  offer_opened boolean NOT NULL DEFAULT false,
  offered_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (user_id, placement)
);

CREATE TABLE IF NOT EXISTS public.chat_topic_entries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  placement text NOT NULL,
  offer_id uuid NOT NULL,
  offer_sequence bigint NOT NULL,
  topic_id text NOT NULL,
  topic_revision text NOT NULL,
  questions jsonb NOT NULL,
  context_revision bigint NOT NULL,
  state text NOT NULL DEFAULT 'pending',
  timezone_name text NOT NULL,
  local_date date NOT NULL,
  created_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  committed_message_id bigint,
  first_user_message_id bigint,
  FOREIGN KEY (user_id, committed_message_id) REFERENCES public.messages(user_id, id) ON DELETE CASCADE,
  FOREIGN KEY (user_id, first_user_message_id) REFERENCES public.messages(user_id, id) ON DELETE CASCADE,
  CONSTRAINT topic_entry_state CHECK (state IN ('pending','committed','superseded')),
  CONSTRAINT topic_entry_expiration CHECK (expires_at > created_at),
  CONSTRAINT topic_entry_committed_links CHECK (
    state <> 'committed' OR (committed_message_id IS NOT NULL AND first_user_message_id IS NOT NULL)
  ),
  CONSTRAINT topic_entry_pending_links CHECK (
    state <> 'pending' OR (committed_message_id IS NULL AND first_user_message_id IS NULL)
  )
);
CREATE UNIQUE INDEX IF NOT EXISTS topic_entry_one_pending
  ON public.chat_topic_entries(user_id) WHERE state = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS topic_entry_one_answer
  ON public.chat_topic_entries(user_id, offer_id) WHERE first_user_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS topic_entry_offer_lookup
  ON public.chat_topic_entries(user_id, offer_id, created_at);
CREATE INDEX IF NOT EXISTS topic_entry_expiry
  ON public.chat_topic_entries(expires_at) WHERE state <> 'committed';

ALTER TABLE public.user_topic_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.chat_topic_entries ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.user_topic_states, public.chat_topic_entries FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON public.user_topic_states, public.chat_topic_entries TO service_role;

-- Existing normal/fortune kinds remain valid. Topic answers commit an opening row.
ALTER TABLE public.messages DROP CONSTRAINT messages_kind_check;
ALTER TABLE public.messages ADD CONSTRAINT messages_kind_check CHECK (kind = ANY (ARRAY['normal'::text, 'greeting'::text, 'fortune_context_root'::text, 'fortune_derived'::text, 'topic_opening'::text]));
