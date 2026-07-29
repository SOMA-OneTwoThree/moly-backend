-- 적용: python db/apply.py db/migrations/20260729_diary_source_none.sql --commit
-- SOMA-389: 프리셋 일기 매일 발행 폐지 — diary_source에 'none'(tombstone) 추가.
--   랜덤 프리셋 폴백을 없애 '임계 미달 + 지정본 없음' 날엔 사용자 노출 일기를 안 만든다.
--   대신 그날 '처리완료' 멱등 마커(+임계 미달 대화자 mem0 재실행 방지)로 source='none',
--   published_at=NULL 행을 남긴다(list/get 일기 API가 published_at<=now로 자동 제외).
BEGIN;

ALTER TABLE public.diaries DROP CONSTRAINT IF EXISTS diaries_source_check;
ALTER TABLE public.diaries ADD CONSTRAINT diaries_source_check
  CHECK (source IN ('llm','preset','welcome','none'));

COMMIT;
