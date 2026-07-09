-- 루틴 요일별 스케줄 지원 — days_of_week 추가(ISO 1=월…7=일, null=주 N회 모드). 하위호환.
ALTER TABLE public.routines ADD COLUMN IF NOT EXISTS days_of_week smallint[];
