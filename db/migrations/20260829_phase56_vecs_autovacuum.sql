-- 적용: python db/apply.py db/migrations/20260829_phase56_vecs_autovacuum.sql --env prod --commit --allow-prod
-- Phase 5-6(로드맵 v4): vecs.moly_memories_v2 autovacuum 평형 — pg_repack(2-3) 직후 적용.
--
-- 정상 상태의 1차 수단은 repack 재실행이 아니라 autovacuum 평형이다. 팽창의 실제 축은
-- TOAST(3072차원 벡터 UPDATE마다 ~12KB 사본)라 본체와 TOAST 양쪽 scale_factor를 낮춘다.
-- 0.02 = 14k행 기준 ~280행 변경마다 vacuum — 기본 0.2(2.8k행)로는 리라이트 사이클을 못 따라간다.
-- 재팽창 감시는 /health/deep의 vecs_bytes_per_row, 임계 초과 시 대응은 로드맵 트리거 표.
ALTER TABLE vecs.moly_memories_v2 SET (
    autovacuum_vacuum_scale_factor = 0.02,
    toast.autovacuum_vacuum_scale_factor = 0.02
);
