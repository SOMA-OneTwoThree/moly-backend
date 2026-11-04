-- 같은 릴리스 준비 plan의 전체 audit 통과 후, 새 API 공개 직전에 실행.
-- 전 사용자에게 한 번에 적용하며 사용자 목록/비율/국가별 분기는 없다.
INSERT INTO public.app_config(key,value,updated_at) VALUES
  ('crisis_guard_policy_version','"crisis-v2"'::jsonb,now()),
  ('crisis_guard_timeout_s','2.0'::jsonb,now()),
  ('crisis_guard_history_messages','6'::jsonb,now()),
  ('crisis_guard_enabled','true'::jsonb,now()),
  ('context_retention_v2_enabled','true'::jsonb,now())
ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at;
