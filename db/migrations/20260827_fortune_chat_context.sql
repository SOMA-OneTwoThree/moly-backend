-- Fortune UI → chat one-shot provenance. Core fortune tables can ship independently.
BEGIN;

ALTER TABLE public.messages DROP CONSTRAINT IF EXISTS messages_kind_check;
ALTER TABLE public.messages
  ADD CONSTRAINT messages_kind_check CHECK (
    kind IN ('normal','greeting','fortune_context_root','fortune_derived')
  );

COMMIT;
