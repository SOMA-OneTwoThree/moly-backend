#!/bin/bash
# 운영 배포 직후 작업 — 필수 3개를 순서대로 하고 매 단계 확인한다.
#
# 절차는 docs/MIGRATION_CHECKLIST.md 3-2절. 하나라도 어긋나면 그 자리에서 멈춘다.
#
# ⚠️ 이 스크립트를 돌리면 **구 코드로 되돌릴 수 없다.**
#    2단계(제약 삭제)와 3단계(트리거 제거)가 구 코드의 일기 저장을 깨뜨린다.
#    돌리기 전에 새 코드로 대화·일기가 정상인지 반드시 확인한다.
#
# 사용:
#   bash scripts/post_deploy.sh          # 미리보기(아무것도 안 바꾼다)
#   bash scripts/post_deploy.sh --apply  # 실제 실행
set -uo pipefail
cd /Users/brownie/Documents/workspace/SoftwareMaestro/moly/moly-backend

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1
PY=.venv/bin/python
ENVF=.env.prod

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n⚠ %s — 중단한다.\n' "$*"; exit 1; }

sql() {  # sql "<질의>"  → 한 값 출력
  $PY - "$1" <<'PY'
import os,sys,asyncio,asyncpg
dsn=os.popen("grep '^SUPABASE_DB_CONNECTION_STRING=' .env.prod | cut -d= -f2-").read().strip()
async def go():
    c=await asyncpg.connect(dsn,statement_cache_size=0)
    print(await c.fetchval(sys.argv[1]))
    await c.close()
asyncio.run(go())
PY
}

run() {  # run "<문장>"  → 실행(--apply 일 때만)
  if [ "$APPLY" = "0" ]; then echo "    [미리보기] $1"; return 0; fi
  $PY - "$1" <<'PY'
import os,sys,asyncio,asyncpg
dsn=os.popen("grep '^SUPABASE_DB_CONNECTION_STRING=' .env.prod | cut -d= -f2-").read().strip()
async def go():
    c=await asyncpg.connect(dsn,statement_cache_size=0)
    await c.execute(sys.argv[1]); await c.close()
asyncio.run(go())
PY
}

# ── 0. 사전 확인 ────────────────────────────────────────────────────────────
say "0. 사전 확인"
VER="$(curl -s -m 10 https://voice.moly.asia/health/ready)"
echo "  운영 헬스: $VER"
echo "$VER" | grep -q '"db":"ok"' || die "운영 서버가 정상이 아니다"

RO="$(sql "SELECT current_setting('default_transaction_read_only')")"
[ "$RO" = "off" ] || die "DB가 읽기 전용이다"
echo "  읽기 전용: $RO"

MAIN_SHA="$(git rev-parse origin/main)"
DEPLOYED="$(echo "$VER" | sed -E 's/.*"version":"([a-f0-9]+)".*/\1/')"
echo "  배포 버전 $DEPLOYED"
echo "  origin/main $MAIN_SHA"
[ "$DEPLOYED" = "$MAIN_SHA" ] || echo "  ⚠ 배포 버전과 origin/main이 다르다 — 배포가 끝났는지 확인할 것"

# ── 1. 빈 구간 메시지 턴 좌표 ───────────────────────────────────────────────
say "1. 빈 구간 메시지에 턴 좌표 매기기"
GAP="$(sql "SELECT count(*) FROM messages WHERE kind='normal' AND turn_seq IS NULL")"
echo "  좌표 없는 메시지: ${GAP}건"
if [ "$GAP" != "0" ]; then
  if [ "$APPLY" = "1" ]; then
    PYTHONPATH=. $PY db/apply.py db/cutover/backfill_gap_turn_seq.sql --env prod --allow-prod --commit | tail -2
  else
    PYTHONPATH=. $PY db/apply.py db/cutover/backfill_gap_turn_seq.sql --env prod | tail -2
  fi
  if [ "$APPLY" = "1" ]; then
    LEFT="$(sql "SELECT count(*) FROM messages WHERE kind='normal' AND turn_seq IS NULL")"
    DUP="$(sql "SELECT count(*) FROM (SELECT user_id,turn_seq,turn_position FROM messages WHERE kind='normal' AND turn_seq IS NOT NULL GROUP BY 1,2,3 HAVING count(*)>1) t")"
    echo "  남은 좌표 없음: $LEFT · 좌표 중복: $DUP"
    [ "$DUP" = "0" ] || die "좌표가 겹쳤다"
  fi
fi

# ── 2. 옛 제약 삭제 ─────────────────────────────────────────────────────────
say "2. diaries_user_date_uq 삭제 (되돌리기가 여기서 끊긴다)"
echo "  안 하면 신규 가입자가 첫 개인일기를 못 받는다."
run "ALTER TABLE public.diaries DROP CONSTRAINT IF EXISTS diaries_user_date_uq"
if [ "$APPLY" = "1" ]; then
  N="$(sql "SELECT count(*) FROM pg_constraint WHERE conname='diaries_user_date_uq'")"
  [ "$N" = "0" ] || die "제약이 남아 있다"
  echo "  삭제 확인"
fi

# ── 3. 호환 트리거 제거 ─────────────────────────────────────────────────────
say "3. 호환 트리거·함수 제거"
run "DROP TRIGGER IF EXISTS diaries_legacy_compat_tg ON public.diaries"
run "DROP FUNCTION IF EXISTS public.diaries_legacy_compat()"
if [ "$APPLY" = "1" ]; then
  N="$(sql "SELECT count(*) FROM pg_trigger WHERE tgname='diaries_legacy_compat_tg'")"
  [ "$N" = "0" ] || die "트리거가 남아 있다"
  echo "  제거 확인"
fi

# ── 4. 게이트 검사 ──────────────────────────────────────────────────────────
say "4. 승격 게이트 검사"
MOLY_ENV_FILE=$ENVF PYTHONPATH=. $PY scripts/verify_cutover_gate.py --env prod 2>&1 | grep -E "^  (✅|❌)" | tail -20

say "다음: 기억 켜기"
cat <<'EOS'
  게이트 결과를 보고 판단한 뒤 승격한다.

    db/cutover/promote_memory_v2.sql 의 1) 미리보기 SELECT 를 먼저 돌려
    '승격 가능' 인원을 확인하고, 2) UPDATE 를 실행한다.

  되돌리기: 같은 파일 3)번으로 mode 를 shadow 로 내리면 기억만 꺼진다(코드는 그대로).
EOS

say "선택 작업 (급하지 않음)"
cat <<'EOS'
  - 못 만든 웰컴 1건 채우기 (2번 뒤라야 들어간다)
  - 처리 표시 재복사 → 빈 행 삭제 (이 순서를 지킨다)
  - 회상 문서 재구축 (빈 구간에 만들어진 일기)
  - vecs.memories 158 MB 삭제 (Pro 한도 8 GB 중 — 몇 주 뒤에 해도 된다)
EOS

[ "$APPLY" = "0" ] && say "미리보기였다. 실제로 하려면 --apply 를 붙인다."
exit 0
