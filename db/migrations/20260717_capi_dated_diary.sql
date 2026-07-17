-- 캐피 날짜별 자기일기(date-specific preset diary) — additive, 무중단.
-- moly_life_ments에 diary_date를 붙여 "그날 지정 일기"를 만든다.
--   · diary_date 있는 행 = 그 날짜 지정본(직접 작성)  → 생성 틱이 우선 선택
--   · diary_date NULL 행  = 기존 랜덤 폴백 풀           → 지정본 없는 날 대신 나감
-- 기존 시드 10행은 컬럼이 없어 전부 NULL → 그대로 폴백 풀로 유지(데이터 이행 불필요).
-- 적용: python db/apply.py db/migrations/20260717_capi_dated_diary.sql --commit

ALTER TABLE public.moly_life_ments ADD COLUMN IF NOT EXISTS diary_date date;

-- 한 날짜당 지정본 1건만(편집은 in-place). NULL 풀 행은 제약 밖(부분 인덱스).
CREATE UNIQUE INDEX IF NOT EXISTS moly_life_ments_diary_date_uq
  ON public.moly_life_ments (diary_date) WHERE diary_date IS NOT NULL;
