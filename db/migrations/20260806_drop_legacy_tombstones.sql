-- 적용: python db/apply.py db/migrations/20260806_drop_legacy_tombstones.sql --commit
-- legacy→v2 이관 다리 제거.
--
-- 이 표는 legacy 기억에서 "지워달라고 한 것"을 v2로 넘길 때 쓰는 다리였다. 원본 legacy
-- 구조가 사라졌으므로(20260806_drop_legacy_memory.sql) 넘길 것도, 대조할 것도 없다.
-- dev 확인: 0행.
BEGIN;
DROP TABLE IF EXISTS public.legacy_recall_tombstones;
COMMIT;
