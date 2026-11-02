-- Never remove persisted access/financial stages or deploy an unaware old image.
-- After adoption, disable NEW adoption and retain the compatible reader/writer.
DO $$
BEGIN
    IF current_setting('moly.subscription_readers_and_writers_stopped', true)
       IS DISTINCT FROM 'confirmed' THEN
        RAISE EXCEPTION 'Stop all compatible readers and writers; explicit confirmation required';
    END IF;
END $$;
LOCK TABLE public.revenuecat_events, public.subscriptions IN ACCESS EXCLUSIVE MODE;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.subscriptions WHERE access_evidence IS NOT NULL)
       OR EXISTS (SELECT 1 FROM public.revenuecat_events WHERE processing_state IS NOT NULL) THEN
        RAISE EXCEPTION 'Persisted subscription stages exist; retain columns and compatible code';
    END IF;
END $$;
ALTER TABLE public.revenuecat_events DROP COLUMN processing_state;
ALTER TABLE public.subscriptions DROP COLUMN access_evidence;
