-- 전체 API/구버전 worker를 닫고 lease를 drain한 점검 시간에만 실행.
-- 공개 전 기존 자료를 새 worker로 준비한다. 사용자별 목록은 사용하지 않는다.
INSERT INTO public.app_config(key,value,updated_at) VALUES
  ('crisis_guard_enabled','false'::jsonb,now()),
  ('context_retention_v2_enabled','true'::jsonb,now())
ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at;
