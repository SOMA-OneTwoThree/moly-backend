-- Additive, dev first; prod only as a separately approved release operation.
-- NULL=unseen, 0001-01-01=legacy used, other date=first successful result day.
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS fortune_first_date date;
COMMENT ON COLUMN public.profiles.fortune_first_date IS 'First fortune day; 0001-01-01 means legacy use; internal, survives fortune profile deletion';
UPDATE public.profiles p SET fortune_first_date = DATE '0001-01-01'
WHERE p.fortune_first_date IS NULL
AND EXISTS (SELECT 1 FROM public.daily_fortunes d WHERE d.user_id=p.id);
