#!/bin/bash
# 기억 백필을 100명씩 나눠 돌린다 (운영 전환 1.5단계, 1회성).
#
# 한 묶음 = 등록(enter_shadow_cohort) → 추출(run_memory_consumer) → 확인.
# 실패 작업이 하나라도 생기면 그 자리에서 멈춘다.
#
# 사용:
#   bash scripts/backfill_memory_batches.sh 100      # 묶음 크기 100
set -u
cd /Users/brownie/Documents/workspace/SoftwareMaestro/moly/moly-backend

SIZE="${1:-100}"
export MOLY_ENV_FILE=.env.prod
export JOB_MEMORY_CONCURRENCY=8
export JOB_MEMORY_CLAIM_BATCH=8
export PYTHONPATH=.

stat() {
  .venv/bin/python - <<'PY'
import os,asyncio,asyncpg
dsn=os.popen("grep '^SUPABASE_DB_CONNECTION_STRING=' .env.prod | cut -d= -f2-").read().strip()
async def go():
    c=await asyncpg.connect(dsn,statement_cache_size=0)
    st=await c.fetchval("SELECT count(*) FROM memory_pipeline_states")
    done=await c.fetchval("SELECT count(*) FROM memory_pipeline_states WHERE ingest_through_turn_seq=source_through_turn_seq")
    mem=await c.fetchval("SELECT count(*) FROM mem0_memory_registry")
    dead=await c.fetchval("SELECT count(*) FROM async_jobs WHERE queue='memory' AND state='dead'")
    db=await c.fetchval("SELECT pg_database_size('postgres')")
    turns=int(await c.fetchval("SELECT coalesce(sum(ingest_through_turn_seq),0) FROM memory_pipeline_states"))
    print(f"{st} {done} {mem} {dead} {db/1048576:.1f} {turns}")
    await c.close()
asyncio.run(go())
PY
}

BATCH=0
while true; do
  BATCH=$((BATCH+1))

  OUT="$(.venv/bin/python scripts/enter_shadow_cohort.py --env prod --limit "$SIZE" --yes 2>&1)"
  if echo "$OUT" | grep -q "대상 사용자가 없다"; then
    echo "[$(date +%H:%M:%S)] 남은 대상 없음 — 전부 끝났다."
    break
  fi
  N="$(echo "$OUT" | grep -c '✅')"
  echo "[$(date +%H:%M:%S)] 묶음 $BATCH — ${N}명 등록, 추출 시작"

  S=$(date +%s)
  .venv/bin/python scripts/run_memory_consumer.py --until-empty >/dev/null 2>&1
  E=$(( $(date +%s) - S ))

  read -r ST DONE MEM DEAD DB TURNS <<<"$(stat)"
  echo "[$(date +%H:%M:%S)] 묶음 $BATCH 완료 — ${E}초 · 등록 $ST · 완료 $DONE · 기억 $MEM · 턴 $TURNS · DB ${DB}MB · 실패 $DEAD"

  if [ "${DEAD:-0}" != "0" ]; then
    echo "⚠ 실패 작업 $DEAD 건 — 중단한다."
    exit 1
  fi
done

read -r ST DONE MEM DEAD DB TURNS <<<"$(stat)"
echo "=== 전체 완료 — 등록 $ST · 완료 $DONE · 기억 $MEM · 턴 $TURNS · DB ${DB}MB · 실패 $DEAD ==="
