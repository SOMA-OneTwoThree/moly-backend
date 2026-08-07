-- 기억을 응답에 쓰기 시작한다 — `shadow` → `v2` 승격.
--
-- ⚠️ **운영 서버에 새 코드가 배포된 뒤에 실행한다.** 구 코드는 이 값을 읽지 않으므로
--    배포 전에 올려도 아무 일이 안 일어나고, 되돌릴 때만 헷갈린다.
--
-- 왜 SQL인가
--   `enter_shadow()`·`mark_bootstrap_ready()`와 달리 `shadow` → `v2` 승격은 **코드에 경로가
--   없다**(2026-08-07 확인: `MODE_V2`를 쓰는 곳은 판정 속성과 검증 질의뿐). 그래서 값을 직접
--   바꾼다.
--
-- 무엇이 달라지나
--   `serves_v2`가 `mode = 'v2'`일 때만 참이다(`memory_pipeline.py` 55~57행). `shadow`는
--   기억을 쌓기만 하고 응답에 쓰지 않는다. `v2`로 올리는 순간부터 회상 결과가 프롬프트에
--   들어간다.
--
-- 실행 전에
--   `PYTHONPATH=. uv run python scripts/verify_cutover_gate.py --env prod` 를 먼저 돌린다.
--   그 도구도 "자동 항목이 전부 통과해도 조건을 만족한 게 아니다"라고 적어 두었다.


-- ── 1) 미리보기 — 누가 올라가고 누가 안 올라가는지 ──────────────────────────
-- 실행하지 않고 이 SELECT만 먼저 본다.
SELECT
  CASE
    WHEN s.mode <> 'shadow'                                          THEN 'shadow가 아님'
    WHEN s.bootstrap_status <> 'ready'                               THEN '초기적재 미완료'
    WHEN s.ingest_through_turn_seq <> s.source_through_turn_seq      THEN '추출이 안 따라잡음'
    WHEN s.consolidated_through_turn_seq <> s.ingest_through_turn_seq THEN '정리가 안 따라잡음'
    WHEN s.lease_until IS NOT NULL AND s.lease_until > now()         THEN '작업 진행 중'
    WHEN EXISTS (SELECT 1 FROM public.mem0_memory_registry r
                 WHERE r.user_id = s.user_id AND r.semantic_status = 'pending')
                                                                     THEN '판정 대기 기억 있음'
    WHEN EXISTS (SELECT 1 FROM public.mem0_ingest_candidates k
                 WHERE k.user_id = s.user_id AND k.status = 'planned'
                   AND k.created_at < now() - interval '30 minutes')  THEN '닫히지 않은 후보 있음'
    ELSE '승격 가능'
  END AS 판정,
  count(*) AS 인원
FROM public.memory_pipeline_states s
GROUP BY 1
ORDER BY 2 DESC;


-- ── 2) 승격 — 위 미리보기에서 '승격 가능'인 사람만 올라간다 ────────────────
-- 조건을 UPDATE 안에 그대로 넣어서, 미리보기와 실제 대상이 어긋나지 않게 한다.
--
-- 일부만 먼저 올리려면 맨 아래 `AND s.user_id IN (...)` 주석을 푼다.

BEGIN;

UPDATE public.memory_pipeline_states s
SET mode = 'v2',
    revision = s.revision + 1,
    updated_at = now()
WHERE s.mode = 'shadow'
  AND s.bootstrap_status = 'ready'
  AND s.ingest_through_turn_seq = s.source_through_turn_seq
  AND s.consolidated_through_turn_seq = s.ingest_through_turn_seq
  AND (s.lease_until IS NULL OR s.lease_until <= now())
  AND NOT EXISTS (
        SELECT 1 FROM public.mem0_memory_registry r
        WHERE r.user_id = s.user_id AND r.semantic_status = 'pending')
  AND NOT EXISTS (
        SELECT 1 FROM public.mem0_ingest_candidates k
        WHERE k.user_id = s.user_id AND k.status = 'planned'
          AND k.created_at < now() - interval '30 minutes')
  -- AND s.user_id IN ('...', '...')   -- 일부만 올릴 때 푼다
;

-- 결과 확인 후 COMMIT 한다. 숫자가 예상과 다르면 ROLLBACK.
SELECT mode, count(*) FROM public.memory_pipeline_states GROUP BY 1 ORDER BY 2 DESC;

COMMIT;


-- ── 3) 되돌리기 — 기억 품질이 이상할 때 ────────────────────────────────────
-- 코드를 되돌리지 않고 기억만 끄는 방법이다. 쌓인 기억은 그대로 두고 응답에서만 뺀다.
--
-- BEGIN;
-- UPDATE public.memory_pipeline_states
-- SET mode = 'shadow', revision = revision + 1, updated_at = now()
-- WHERE mode = 'v2';
-- COMMIT;
