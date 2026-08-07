-- 적용: python db/apply.py db/migrations/20260806_drop_legacy_memory.sql --commit
-- legacy 정규화 기억 구조 제거 (기억 재설계 13단계).
--
-- ⚠️ **이 마이그레이션은 되돌릴 수 없다.** 적용 전 반드시 확인한다:
--   1. 모든 사용자가 mode='v2'인가
--   2. chat이 legacy를 읽지도 쓰지도 않는가 (v2 분기로 건너뛴다)
--   3. 실제 대화가 legacy 없이 도는가
--
-- dev 확인(2026-08-06): 사용자 3명 전부 v2 · 대화 2턴 정상 · memory_source_turns 111 → 111.
--
-- 🔴 prod에는 아직 적용하지 않는다. prod는 이 구조 자체가 없고(테이블 25개), v2 백필과
--    cutover가 끝난 뒤에야 이 파일이 의미를 갖는다.
BEGIN;

-- 순서는 DB의 실제 FK로 계산했다(위상정렬). CASCADE를 쓰지 않는다 — 예상 못 한 테이블까지
-- 딸려가면 무엇이 사라졌는지 나중에 알 수 없다.
--
-- 이 13개는 **서로만** 참조한다. 바깥에서 이들을 참조하는 테이블은 없음을 확인했다.
DROP TABLE IF EXISTS public.memory_evidence;
DROP TABLE IF EXISTS public.memory_insight_sources;
DROP TABLE IF EXISTS public.memory_source_turn_messages;
DROP TABLE IF EXISTS public.memory_source_closures;
DROP TABLE IF EXISTS public.memory_episodic_messages;
DROP TABLE IF EXISTS public.memory_forget_markers;
DROP TABLE IF EXISTS public.memory_recall_suppressions;
DROP TABLE IF EXISTS public.memory_suppression_operations;
DROP TABLE IF EXISTS public.relationship_profile_sources;
DROP TABLE IF EXISTS public.memory_facts;
DROP TABLE IF EXISTS public.memory_insights;
DROP TABLE IF EXISTS public.memory_source_turns;
DROP TABLE IF EXISTS public.relationship_profiles;

COMMIT;
