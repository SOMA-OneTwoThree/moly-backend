"""운영 전환 전 점검. **읽기만 한다** — 어떤 DB에 걸어도 안전하다.

마이그레이션이 실패하는 이유는 대개 SQL이 틀려서가 아니라 **그 DB의 데이터가 새 제약을
못 맞춰서**다. 그건 적용해 보기 전에는 안 드러나고, 절반쯤 적용된 상태에서 드러나면 최악이다.
이 스크립트가 그 조건들을 미리 센다.

각 항목은 "0이어야 한다"로 통일했다 — 0이 아니면 그 수만큼 고쳐야 넘어간다.

사용:
    PYTHONPATH=. uv run python scripts/preflight_cutover.py            # dev
    PYTHONPATH=. uv run python scripts/preflight_cutover.py --env prod # prod (읽기 전용)
"""
from __future__ import annotations

import asyncio
import sys

import asyncpg

from db.envfile import announce, load_conn, split_env_arg

# (이름, SQL, 설명) — SQL은 정수 하나를 돌려주고 0이 통과다.
# `to_regclass`로 감싼 항목은 해당 테이블이 아직 없는 DB(전환 전 prod)에서도 돌아간다.
CHECKS: list[tuple[str, str, str]] = [
    (
        "일기 날짜 없음",
        "SELECT count(*) FROM diaries WHERE diary_date IS NULL",
        "display_date NOT NULL 백필이 diary_date를 그대로 쓴다 — NULL이면 제약 추가가 실패한다",
    ),
    (
        "welcome 일기 중복",
        "SELECT count(*) FROM (SELECT user_id FROM diaries WHERE source='welcome' "
        "GROUP BY 1 HAVING count(*)>1) x",
        "diaries_one_welcome_uq(user_id, kind='welcome')가 사용자당 1건만 허용한다",
    ),
    (
        "하루 일기 중복",
        "SELECT count(*) FROM (SELECT user_id,diary_date FROM diaries "
        "WHERE source IN ('llm','preset') GROUP BY 1,2 HAVING count(*)>1) x",
        "diaries_one_daily_uq(user_id, activity_date)가 하루 1건만 허용한다",
    ),
    (
        "알 수 없는 일기 source",
        "SELECT count(*) FROM diaries WHERE source NOT IN ('none','preset','llm','welcome')",
        "source→kind 매핑에 없는 값은 kind가 NULL이 되어 부분 인덱스가 그 행을 보호하지 못한다",
    ),
    (
        "타임존 없는 프로필",
        "SELECT count(*) FROM profiles WHERE timezone IS NULL",
        "activity_date 계산이 타임존을 요구한다",
    ),
    (
        "주인 없는 메시지",
        "SELECT count(*) FROM messages m WHERE NOT EXISTS "
        "(SELECT 1 FROM profiles p WHERE p.id=m.user_id)",
        "신규 FK가 고아 행을 거부한다",
    ),
    (
        "주인 없는 루틴 완료",
        "SELECT count(*) FROM routine_completions rc WHERE NOT EXISTS "
        "(SELECT 1 FROM routines r WHERE r.id=rc.routine_id AND r.user_id=rc.user_id)",
        "routine_completions→routines 복합 FK가 추가된다",
    ),
    (
        "짝 없는 대화 메시지",
        "SELECT abs(count(*) FILTER (WHERE sender='user') - count(*) FILTER (WHERE sender='moly')) "
        "FROM messages WHERE kind='normal'",
        "턴 백필은 사용자 발화 1 + 캐피 응답 1을 한 턴으로 본다. 짝이 안 맞으면 남는 쪽이 "
        "턴을 못 이룬다(경고 — 소수면 무시해도 되지만 수를 알고 넘어가야 한다)",
    ),
    (
        "좌표 없는 대화 메시지",
        "SELECT count(*) FROM messages WHERE kind='normal' AND turn_seq IS NULL",
        "기억 파이프라인이 turn_seq 없는 메시지를 영영 못 본다. "
        "20260806_backfill_turn_seq.sql 적용 **후** 0이어야 한다",
    ),
    (
        "기억이 없는 턴을 가리킴",
        "SELECT count(*) FROM mem0_memory_sources s WHERE NOT EXISTS "
        "(SELECT 1 FROM messages m WHERE m.user_id=s.user_id AND m.kind='normal' "
        "AND m.turn_seq=s.source_turn_seq)",
        "턴을 밀어 올릴 때 참조 테이블을 같이 옮기지 않으면 여기가 깨진다",
    ),
    (
        "커서가 최대 턴 초과",
        "SELECT count(*) FROM memory_pipeline_states s WHERE s.ingest_through_turn_seq > "
        "COALESCE((SELECT max(turn_seq) FROM messages m WHERE m.user_id=s.user_id "
        "AND m.kind='normal'),0)",
        "커서가 존재하지 않는 턴을 가리키면 그 사용자는 더 이상 진행하지 않는다",
    ),
    (
        "legacy에 묶인 사용자",
        "SELECT count(*) FROM profiles p WHERE EXISTS "
        "(SELECT 1 FROM messages m WHERE m.user_id=p.id AND m.kind='normal') "
        "AND COALESCE((SELECT mode FROM memory_pipeline_states s WHERE s.user_id=p.id),'legacy')"
        " = 'legacy'",
        "legacy 읽기 경로는 삭제됐다 — 이 사용자들은 기억이 **비어 있다**. "
        "전환 마지막 단계에서 scripts/enter_shadow_cohort.py로 옮겨야 한다",
    ),
]


async def main(env: str | None) -> int:
    dsn = load_conn(env)
    announce(env, dsn)
    c = await asyncpg.connect(dsn, statement_cache_size=0)
    tx = c.transaction()
    await tx.start()
    failed = 0
    try:
        await c.execute("SET TRANSACTION READ ONLY")
        print()
        for name, sql, why in CHECKS:
            try:
                n = int(await c.fetchval(sql) or 0)
            except (
                asyncpg.exceptions.UndefinedTableError,
                asyncpg.exceptions.UndefinedColumnError,
            ):
                # 전환 전 DB에는 아직 없는 표·컬럼이다. 못 재는 것과 통과는 다르므로 구분해 찍는다.
                await tx.rollback()  # 실패한 문장이 트랜잭션을 중단시킨다 — 다시 연다
                tx = c.transaction()
                await tx.start()
                await c.execute("SET TRANSACTION READ ONLY")
                print(f"  ⏭  {name}: 이 DB에 아직 없는 표/컬럼 — 마이그레이션 후 다시 볼 것")
                continue
            if n:
                failed += 1
                print(f"  ❌ {name}: {n}")
                print(f"       {why}")
            else:
                print(f"  ✅ {name}")
    finally:
        await tx.rollback()
        await c.close()

    print()
    if failed:
        print(f"{failed}개 항목이 걸렸다. 위 설명대로 정리한 뒤 다시 돌린다.")
        return 1
    print("전부 통과.")
    return 0


_env, _ = split_env_arg(sys.argv[1:])
raise SystemExit(asyncio.run(main(_env)))
