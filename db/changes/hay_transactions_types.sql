-- Review/apply using db.apply; never apply schema.sql to an existing DB.
-- One hay ledger type list for every environment: main's types (incl. mood_reward) plus the
-- explore types (training_reward, article_reward). Only widens the list, so existing rows and
-- old images stay valid. Revalidation scans hay_transactions under a brief ACCESS EXCLUSIVE lock.
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
ALTER TABLE public.hay_transactions DROP CONSTRAINT IF EXISTS hay_transactions_type_check;
ALTER TABLE public.hay_transactions ADD CONSTRAINT hay_transactions_type_check CHECK ((type = ANY (ARRAY['attendance'::text, 'ad_reward'::text, 'routine_reward'::text, 'mood_reward'::text, 'iap_purchase'::text, 'subscription_grant'::text, 'shop_purchase'::text, 'refund_revoke'::text, 'admin_adjustment'::text, 'training_reward'::text, 'article_reward'::text])));
