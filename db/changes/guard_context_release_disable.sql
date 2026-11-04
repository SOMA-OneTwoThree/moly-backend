-- 1차 복구: 새 위기 분기와 새 보존 작업의 예약/게시를 전역 중지한다.
-- 이미 게시된 v5를 읽는 호환 경로와 원문/기억/일기/요약은 유지한다.
-- 이 SQL만으로 구버전 바이너리로 복귀할 수 있다는 뜻은 아니다.
INSERT INTO public.app_config(key,value,updated_at) VALUES
  ('crisis_guard_enabled','false'::jsonb,now()),
  ('context_retention_v2_enabled','false'::jsonb,now())
ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at;
