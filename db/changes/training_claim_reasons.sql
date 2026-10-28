-- Expand feedback categories; existing values and rows remain valid.
ALTER TABLE public.training_claims DROP CONSTRAINT training_claims_reason_check;
ALTER TABLE public.training_claims ADD CONSTRAINT training_claims_reason_check
  CHECK (reason IN ('should_pass', 'should_fail', 'wrong_hint', 'technical_error', 'other'));
