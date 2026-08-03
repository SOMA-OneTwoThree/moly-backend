"""legacy → normalized cutover(W10) — **메커니즘만**. 실행(어느 cohort를 언제)은 운영 판단이다.

명세 1295-1311행의 7단계 중 코드로 만들 수 있는 부분이다:

| 단계 | 여기서 |
|---|---|
| 1. 전 프로세스 heartbeat 대조 | `assert_release_inventory` — **운영 절차**(배포 인벤토리 확인). 게이트 자리만 있고 판단은 호출자가 넘긴다 |
| 2. source turn backfill + watermark 일치 | `REASON_SOURCE_BACKFILL_INCOMPLETE` 검사 |
| 3. shadow extract/reconcile 완료 + 해당 locale published 프로필 | `REASON_SHADOW_INCOMPLETE` · `REASON_NO_PUBLISHED_PROFILE` |
| 4. 짧은 트랜잭션에서 `FOR UPDATE` + 조건 fresh read | `cutover_user` |
| 5. 같은 UPDATE로 mode/generation/스냅샷 확정 | `_CUTOVER_SQL` 한 문장 |
| 6~7. 읽기 경로 전환·cohort 확대 | 호출측·운영(여기 아님) |

**downgrade 경로가 없다.** 이 모듈에도, 레포 어디에도 `memory_mode='legacy'`로 되돌리는 UPDATE를
두지 않는다. 롤백은 "미전환 유저를 legacy에 두는 것"이지 "전환된 유저를 되돌리는 것"이 아니다
(명세 §4 롤백표). DB의 `chat_contexts_normalized_snapshot_guard` 트리거가 마지막 방어선이다.

전제를 하나라도 못 채우면 그 유저는 **legacy에 남는다**(부분 전환 없음). 커밋하지 않는다 —
호출측 트랜잭션 경계에 합류한다(운영 스크립트가 유저 1명 = 짧은 트랜잭션 1개로 돈다).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import i18n, memory, memory_repo

_log = logging.getLogger("moly-backend")

# 결과·차단 사유 코드(계약) — 운영 리포트가 이 값으로 집계한다.
RESULT_CUTOVER = "cutover"
RESULT_ALREADY_NORMALIZED = "already_normalized"
RESULT_BLOCKED = "blocked"

REASON_NO_CONTEXT = "no_chat_context"                       # 대화가 한 번도 없는 유저
REASON_SOURCE_BACKFILL_INCOMPLETE = "source_backfill_incomplete"  # 2단계 미완
REASON_SHADOW_INCOMPLETE = "shadow_extract_incomplete"      # 3단계 미완(미처리·dead·watermark 미달)
REASON_NO_PUBLISHED_PROFILE = "no_published_profile"        # 그 locale의 W9 산출물 없음

# reconcile이 **실제로 반영을 끝낸** 결과 코드. stale_generation·source_range_closed도 succeeded지만
# 그건 "결과를 버렸다"는 뜻이라 shadow 완료로 세면 안 된다(worker/memory_jobs.py의 사유 코드).
_DONE_RESULT_CODES = ("ok", "no_candidates")


class CutoverBlocked(Exception):
    """운영 게이트가 닫혀 있다 — 전 cohort 중단(명세 1298행)."""


@dataclass(frozen=True, slots=True)
class CutoverDecision:
    """전제 검사 결과. `reasons`가 비어야만 전환한다."""

    eligible: bool
    reasons: tuple[str, ...]
    locale: str
    source_watermark: int = 0
    memory_mode: str = memory.MODE_LEGACY


@dataclass(frozen=True, slots=True)
class CutoverResult:
    status: str
    user_id: uuid.UUID | str
    locale: str
    reasons: tuple[str, ...] = ()
    memory_generation: int | None = None


# ─────────────────────────────────────────────────────────────
# 1단계 게이트 — **운영 절차**다.
# ─────────────────────────────────────────────────────────────
def assert_release_inventory(confirmed: bool) -> None:
    """API-A/API-B/consumer-A/consumer-B가 모두 같은 mode-aware release인지 확인했는가.

    ⚠️ 이 판단은 **코드 범위 밖**이다 — 살아 있는 프로세스 목록은 배포 인벤토리(ECR 이미지 태그·
    systemd 유닛·헬스체크의 `git_sha`)에만 있고, DB에는 "구버전이 아직 떠 있다"는 사실이 남지
    않는다. 그래서 운영자가 확인한 결과를 인자로 받는다(기본값 없음 = 실수로 통과하지 않는다).
    하나라도 구버전·누락·stale이면 **전 cohort gate를 닫는다**(명세 1297-1298행).
    """
    if not confirmed:
        raise CutoverBlocked(
            "배포 인벤토리 확인 전에는 cutover하지 않는다 — 전 프로세스가 같은 mode-aware "
            "release인지 확인한 뒤 release_inventory_confirmed=True로 호출한다"
        )


def assert_profile_block_wired() -> None:
    """관계 프로필이 실제로 대화에 실리는가.

    normalized 유저는 mem0로 되돌아가지 않는다(그게 이 전환의 요지다). 그래서 관계 프로필이
    프롬프트에 안 들어가면 **기억이 영구히 빈 채로 남는다** — 유저 입장에선 캐피가 자기를
    통째로 잊은 것이다. 되돌릴 수도 없다(downgrade 금지).

    프로필 블록은 아직 배선 전이라 플래그가 꺼져 있다(`prompts.RELATIONSHIP_PROFILE_BLOCK_ENABLED`).
    배선이 끝나 플래그가 켜지기 전에는 cutover를 열지 않는다.
    """
    from app.services import prompts  # 순환 import 회피 — 이 게이트에서만 필요하다

    if not prompts.RELATIONSHIP_PROFILE_BLOCK_ENABLED:
        raise CutoverBlocked(
            "관계 프로필이 대화에 배선되지 않았다 — 지금 cutover하면 normalized 유저의 기억이 "
            "영구히 빈 채로 남는다(mem0 폴백 금지 + downgrade 금지). "
            "prompts.RELATIONSHIP_PROFILE_BLOCK_ENABLED가 켜진 뒤에 연다"
        )


# ─────────────────────────────────────────────────────────────
# 전제 검사 SQL — 전부 유저 스코프.
# ─────────────────────────────────────────────────────────────
_LOCK_CONTEXT_SQL = text("""
SELECT memory_mode, memory_generation, memory_source_watermark
FROM chat_contexts WHERE user_id=:user_id FOR UPDATE
""")

_CONTEXT_SQL = text("""
SELECT memory_mode, memory_generation, memory_source_watermark
FROM chat_contexts WHERE user_id=:user_id
""")

# 2단계 — backfill된 turn의 최대 watermark가 컨텍스트의 watermark와 같아야 한다.
_SOURCE_WATERMARK_SQL = text("""
SELECT COALESCE(max(source_watermark), 0) FROM memory_source_turns WHERE user_id=:user_id
""")

# 3단계 — 미처리(ready/running)·dead가 0이고, reconcile이 마지막 turn까지 끝났는지.
_SHADOW_SQL = text("""
SELECT
  count(*) FILTER (WHERE state IN ('ready','running')) AS pending,
  count(*) FILTER (WHERE state='dead') AS dead,
  COALESCE(max((payload->>'source_through_watermark')::bigint)
           FILTER (WHERE job_type=:reconcile_type AND state='succeeded'
                     AND result_code = ANY(CAST(:done_codes AS text[]))), 0) AS done_watermark
FROM async_jobs
WHERE user_id=:user_id AND job_type = ANY(CAST(:job_types AS text[]))
""")

# 3단계 — 그 locale의 published 관계 프로필(W9 산출물)이 있어야 한다. 없으면 전환 직후 기억이
# 통째로 비고, normalized는 legacy 문자열로 fallback하지 않으므로 유저 경험이 무너진다.
_PUBLISHED_PROFILE_SQL = text("""
SELECT 1 FROM relationship_profiles
WHERE user_id=:user_id AND locale=:locale AND status='published' LIMIT 1
""")

_LANGUAGE_SQL = text("SELECT language FROM profiles WHERE id=:user_id")

# 5단계 — 한 문장으로 확정한다. `memory_mode='legacy'`와 watermark를 WHERE에 두어
# (잠금 밖에서 불렸더라도) 조건이 바뀌었으면 0행으로 조용히 실패한다. 되돌리는 방향은 없다.
_CUTOVER_SQL = text("""
UPDATE chat_contexts
SET memory_mode='normalized',
    memory_generation = memory_generation + 1,
    memory_text = NULL,
    memory_refreshed_at = NULL,
    updated_at = now()
WHERE user_id=:user_id AND memory_mode='legacy' AND memory_source_watermark=:source_watermark
RETURNING memory_generation
""")


async def _locale_of(session: AsyncSession, user_id: uuid.UUID | str) -> str:
    row = (await session.execute(_LANGUAGE_SQL, {"user_id": user_id})).first()
    return i18n.resolve(row[0] if row is not None else None)


async def evaluate(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    locale: str | None = None,
    lock: bool = False,
) -> CutoverDecision:
    """전제 2·3단계를 검사한다(1단계는 `assert_release_inventory`, 4단계는 `cutover_user`).

    `lock=True`면 `chat_contexts` 행을 `FOR UPDATE`로 잠근 채 읽는다 — 전환 트랜잭션에서 쓰는 경로다.
    사유가 하나라도 있으면 그 유저는 legacy에 남는다(부분 전환·강제 전환 경로 없음).
    """
    resolved_locale = locale or await _locale_of(session, user_id)
    sql = _LOCK_CONTEXT_SQL if lock else _CONTEXT_SQL
    row = (await session.execute(sql, {"user_id": user_id})).first()
    if row is None:
        return CutoverDecision(
            eligible=False, reasons=(REASON_NO_CONTEXT,), locale=resolved_locale
        )
    mode, _generation, watermark = row[0], int(row[1]), int(row[2])
    reasons: list[str] = []

    turns_watermark = int(
        (await session.execute(_SOURCE_WATERMARK_SQL, {"user_id": user_id})).scalar_one()
    )
    if turns_watermark != watermark:
        reasons.append(REASON_SOURCE_BACKFILL_INCOMPLETE)

    shadow = (
        await session.execute(
            _SHADOW_SQL,
            {
                "user_id": user_id,
                "reconcile_type": memory_repo.JOB_MEMORY_RECONCILE,
                "job_types": [memory_repo.JOB_MEMORY_EXTRACT, memory_repo.JOB_MEMORY_RECONCILE],
                "done_codes": list(_DONE_RESULT_CODES),
            },
        )
    ).first()
    pending, dead, done_watermark = int(shadow[0]), int(shadow[1]), int(shadow[2])
    if pending or dead or done_watermark != watermark:
        reasons.append(REASON_SHADOW_INCOMPLETE)

    published = (
        await session.execute(
            _PUBLISHED_PROFILE_SQL, {"user_id": user_id, "locale": resolved_locale}
        )
    ).first()
    if published is None:
        reasons.append(REASON_NO_PUBLISHED_PROFILE)

    return CutoverDecision(
        eligible=not reasons,
        reasons=tuple(reasons),
        locale=resolved_locale,
        source_watermark=watermark,
        memory_mode=mode,
    )


async def cutover_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    release_inventory_confirmed: bool,
    locale: str | None = None,
) -> CutoverResult:
    """유저 1명을 normalized로 전환한다(4·5단계). 커밋하지 않는다 — 호출측이 짧게 커밋한다.

    순서가 계약이다: 인벤토리 게이트 → 행 `FOR UPDATE` 잠금 → 전제 fresh read → 같은 UPDATE로
    mode/generation/스냅샷 확정. 이미 normalized면 아무것도 하지 않는다(재전환으로 generation을
    올리면 진행 중인 잡이 통째로 stale이 된다).
    """
    assert_release_inventory(release_inventory_confirmed)
    assert_profile_block_wired()

    decision = await evaluate(session, user_id=user_id, locale=locale, lock=True)
    if decision.memory_mode == memory.MODE_NORMALIZED:
        return CutoverResult(
            status=RESULT_ALREADY_NORMALIZED, user_id=user_id, locale=decision.locale
        )
    if not decision.eligible:
        _log.info(
            "cutover 보류(legacy 유지) — user=%s reasons=%s", user_id, ",".join(decision.reasons)
        )
        return CutoverResult(
            status=RESULT_BLOCKED,
            user_id=user_id,
            locale=decision.locale,
            reasons=decision.reasons,
        )

    row = (
        await session.execute(
            _CUTOVER_SQL,
            {"user_id": user_id, "source_watermark": decision.source_watermark},
        )
    ).first()
    if row is None:  # 잠근 채로 읽었는데 조건이 바뀌었다 = 전환하지 않는다(강제 경로 없음)
        _log.warning("cutover UPDATE 0행 — legacy 유지 user=%s", user_id)
        return CutoverResult(
            status=RESULT_BLOCKED,
            user_id=user_id,
            locale=decision.locale,
            reasons=(REASON_SOURCE_BACKFILL_INCOMPLETE,),
        )
    generation = int(row[0])
    _log.info("cutover 완료 — user=%s locale=%s gen=%d", user_id, decision.locale, generation)
    return CutoverResult(
        status=RESULT_CUTOVER,
        user_id=user_id,
        locale=decision.locale,
        memory_generation=generation,
    )


async def cutover_cohort(
    session_factory,
    *,
    user_ids: Sequence[uuid.UUID | str],
    release_inventory_confirmed: bool,
) -> list[CutoverResult]:
    """cohort 전환 — **유저마다 짧은 트랜잭션 하나**(명세 1304행 "짧은 트랜잭션").

    한 트랜잭션으로 묶지 않는다: 긴 트랜잭션이 `chat_contexts` 행 여러 개를 오래 잠그면 그 유저들의
    대화 커밋(watermark 배정)이 그동안 막힌다. 7단계(cohort 확대 판단)는 이 결과를 보고 **사람이**
    한다 — 여기서 자동으로 다음 cohort로 넓히지 않는다.
    """
    assert_release_inventory(release_inventory_confirmed)
    results: list[CutoverResult] = []
    for uid in user_ids:
        async with session_factory() as session:
            result = await cutover_user(
                session, user_id=uid, release_inventory_confirmed=release_inventory_confirmed
            )
            await session.commit()
        results.append(result)
    return results
