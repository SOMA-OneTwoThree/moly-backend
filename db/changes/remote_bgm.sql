-- Review/apply using db.apply; never apply schema.sql to an existing DB.
-- Additive, server-owned audio catalogue. No member rows are changed.
CREATE TABLE public.bgm_tracks (
  id text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9-]{0,63}$'),
  category text NOT NULL CHECK (category IN ('lofi', 'white_noise')),
  title text NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
  title_i18n jsonb CHECK (jsonb_typeof(title_i18n) = 'object'),
  source text NOT NULL CHECK (source IN ('bundled', 'remote')),
  revision text NOT NULL CHECK (revision ~ '^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$'),
  url text,
  sha256 text CHECK (sha256 ~ '^[a-f0-9]{64}$'),
  size_bytes bigint CHECK (size_bytes > 0 AND size_bytes <= 104857600),
  mime_type text CHECK (mime_type IN ('audio/mp4', 'audio/mpeg', 'audio/wav')),
  sort_order integer NOT NULL DEFAULT 0 CHECK (sort_order >= 0),
  is_active boolean NOT NULL DEFAULT false,
  CONSTRAINT bgm_source_metadata CHECK (
    (source = 'bundled' AND category = 'lofi'
      AND id IN ('felt-piano-memories', 'night-rain-on-tokyo', 'midnight-tokyo-rain',
                 'midnight-tokyo-rain-2', 'neon-rain', 'fading-static')
      AND url IS NULL AND sha256 IS NULL AND size_bytes IS NULL AND mime_type IS NULL)
    OR (source = 'remote' AND url IS NOT NULL AND sha256 IS NOT NULL
        AND size_bytes IS NOT NULL AND mime_type IS NOT NULL)
  )
);
ALTER TABLE public.bgm_tracks ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.bgm_tracks FROM PUBLIC, anon, authenticated, service_role;
GRANT ALL ON public.bgm_tracks TO service_role;

-- Preserve the installed mobile bundled IDs; no placeholder remote audio.
INSERT INTO public.bgm_tracks (id, category, title, source, revision, sort_order, is_active) VALUES
('felt-piano-memories', 'lofi', 'Soft Piano', 'bundled', 'bundled-v1', 0, true),
('night-rain-on-tokyo', 'lofi', 'Tokyo Nights', 'bundled', 'bundled-v1', 1, true),
('midnight-tokyo-rain', 'lofi', 'Midnight Drizzle', 'bundled', 'bundled-v1', 2, true),
('midnight-tokyo-rain-2', 'lofi', 'Before Dawn', 'bundled', 'bundled-v1', 3, true),
('neon-rain', 'lofi', 'Neon Streets', 'bundled', 'bundled-v1', 4, true),
('fading-static', 'lofi', 'Old Radio', 'bundled', 'bundled-v1', 5, true)
ON CONFLICT (id) DO NOTHING;
