-- Apply only with the matching backend release; changes the preflight catalog.
-- No campaign activation and no historical payment/grant ownership backfill.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.subscriptions'::regclass
      AND conname = 'subscriptions_user_id_fkey'
      AND confrelid = 'public.profiles'::regclass
      AND confdeltype IN ('c', 'n')
  ) THEN RAISE EXCEPTION 'unexpected subscription owner foreign key; abort'; END IF;
END;
$$;

ALTER TABLE public.subscriptions ALTER COLUMN user_id DROP NOT NULL;
ALTER TABLE public.subscriptions DROP CONSTRAINT subscriptions_user_id_fkey;
ALTER TABLE public.subscriptions ADD CONSTRAINT subscriptions_user_id_fkey
  FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE SET NULL;
ALTER TABLE public.subscriptions
  ADD COLUMN IF NOT EXISTS rc_subscription_id text,
  ADD COLUMN IF NOT EXISTS ownership_changed_at timestamptz;
CREATE INDEX IF NOT EXISTS subscriptions_rc_subscription_id_idx
  ON public.subscriptions(rc_subscription_id);

ALTER TABLE public.payments
  ADD COLUMN IF NOT EXISTS subscription_plan text,
  ADD COLUMN IF NOT EXISTS subscription_hay_grant_id uuid,
  ADD COLUMN IF NOT EXISTS subscription_bonus_review text;
ALTER TABLE public.payments DROP CONSTRAINT IF EXISTS payments_subscription_plan_check;
ALTER TABLE public.payments ADD CONSTRAINT payments_subscription_plan_check
  CHECK (subscription_plan IN ('monthly', 'yearly'));
ALTER TABLE public.payments DROP CONSTRAINT IF EXISTS payments_subscription_hay_grant_id_fkey;
ALTER TABLE public.payments ADD CONSTRAINT payments_subscription_hay_grant_id_fkey
  FOREIGN KEY (subscription_hay_grant_id) REFERENCES public.subscription_hay_grants(id)
  ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS payments_subscription_hay_grant_id_idx
  ON public.payments(subscription_hay_grant_id);

-- Deferred checks observe both CASCADE and SET NULL effects regardless of deletion order.
-- Keep a subscription only while it has an access owner or a surviving payer's history.
CREATE OR REPLACE FUNCTION public.cleanup_unowned_subscription()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v_subscription_id uuid;
BEGIN
  IF TG_TABLE_NAME = 'subscriptions' THEN
    v_subscription_id := NEW.id;
  ELSE
    v_subscription_id := OLD.subscription_id;
  END IF;
  DELETE FROM public.subscriptions s
  WHERE s.id = v_subscription_id AND s.user_id IS NULL
    AND NOT EXISTS (SELECT 1 FROM public.payments p WHERE p.subscription_id = s.id);
  RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION public.cleanup_unowned_subscription() FROM PUBLIC, anon, authenticated, service_role;
DROP TRIGGER IF EXISTS subscriptions_cleanup_unowned ON public.subscriptions;
CREATE CONSTRAINT TRIGGER subscriptions_cleanup_unowned
  AFTER UPDATE OF user_id ON public.subscriptions
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
  EXECUTE FUNCTION public.cleanup_unowned_subscription();
DROP TRIGGER IF EXISTS payments_cleanup_unowned_subscription ON public.payments;
CREATE CONSTRAINT TRIGGER payments_cleanup_unowned_subscription
  AFTER DELETE ON public.payments
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
  EXECUTE FUNCTION public.cleanup_unowned_subscription();

DELETE FROM public.subscriptions s
WHERE s.user_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM public.payments p WHERE p.subscription_id = s.id);
COMMIT;
