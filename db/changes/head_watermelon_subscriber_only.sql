-- Makes the watermelon hat subscriber-only, like the towel.
-- Its only owners are two testers who bought it for 1000 HAY; their rows are removed without a refund.
-- Expected: 2 rows deleted, 1 row updated. Fails if anyone other than the two testers owns it.
-- Rollback: set is_subscriber_only = false and price_hay = 1000. The tester rows are not restored.
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM public.user_items ui
    JOIN public.products p ON p.id = ui.product_id
    WHERE p.public_id = 'head_watermelon'
      AND ui.source <> 'subscription'
      AND ui.user_id NOT IN ('d7a18293-85dd-411b-bc05-23438e236af8', '2ebb057b-7669-4891-b1d0-1bbbb0de4264')
  ) THEN
    RAISE EXCEPTION 'head_watermelon has owners other than the testers';
  END IF;
END $$;

DELETE FROM public.user_items ui
USING public.products p
WHERE p.id = ui.product_id
  AND p.public_id = 'head_watermelon'
  AND ui.user_id IN ('d7a18293-85dd-411b-bc05-23438e236af8', '2ebb057b-7669-4891-b1d0-1bbbb0de4264');

UPDATE public.products
SET is_subscriber_only = true, price_hay = NULL
WHERE public_id = 'head_watermelon';
