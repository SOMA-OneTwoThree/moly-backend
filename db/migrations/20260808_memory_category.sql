-- 기억의 종류(category)를 저장한다.
--
-- 추출기는 후보마다 6종(preference·relationship·concern·emotion·routine_intent·event) 중
-- 하나를 뽑아 왔는데, 지금까지 **어디에도 저장하지 않았다.** 후보마다 출력 토큰을 쓰고,
-- 값이 목록 밖이면 덩어리 전체를 폐기하면서, 얻는 것은 없었다.
--
-- 저장해야 회상에서 오래 남는 종류(취향·관계)를 일회성 사건보다 앞세울 수 있다.
-- 574명 재추출은 한 번뿐이라, 지금 넣지 않으면 나중에 전체를 다시 뽑아야 한다.
--
-- ⚠️ `CHECK (category IN (...))`를 **넣지 않는다.** 모델이 목록 밖 값을 내면 DB 층에서
--    쓰기가 통째로 실패해, 코드에서 막 없앤 "하나 틀리면 전량 폐기"를 다시 만들게 된다.
--    허용 목록은 코드(`mem0_extractor.CATEGORIES`)가 갖고, 목록 밖은 `event`로 흡수한다.

ALTER TABLE public.mem0_ingest_candidates
  ADD COLUMN IF NOT EXISTS category text NULL;

ALTER TABLE public.mem0_memory_registry
  ADD COLUMN IF NOT EXISTS category text NULL;

COMMENT ON COLUMN public.mem0_ingest_candidates.category IS
  '기억의 종류. 허용 목록은 코드가 갖는다(mem0_extractor.CATEGORIES). NULL = v3 이전에 뽑힌 기억.';
COMMENT ON COLUMN public.mem0_memory_registry.category IS
  '기억의 종류. 회상에서 오래 남는 종류를 앞세우는 데 쓴다. NULL = v3 이전에 뽑힌 기억.';

-- 회상이 종류로 거르거나 앞세울 때 쓰는 색인. 살아 있는 기억만 대상이라 부분 색인으로 둔다.
CREATE INDEX IF NOT EXISTS mem0_memory_registry_category_idx
  ON public.mem0_memory_registry (user_id, category)
  WHERE semantic_status IN ('active', 'ambiguous');
