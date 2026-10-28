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
ALTER TABLE public.hay_transactions DROP CONSTRAINT IF EXISTS hay_transactions_type_check;
ALTER TABLE public.hay_transactions ADD CONSTRAINT hay_transactions_type_check CHECK
(type IN ('attendance','ad_reward','routine_reward','iap_purchase','subscription_grant','shop_purchase','refund_revoke','admin_adjustment','training_reward','article_reward'));
