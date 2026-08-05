"""사용자별 schedule 4종 — indexed due scheduler의 좌표(15장 4번).

지금 워커는 매 틱 **전 profile을 훑어** 각자의 로컬 시각을 계산한다. 사용자가 늘면 그 비용이
선형으로 늘고, 15분 케이던스 안에 못 돌면 그날 작업이 밀린다. `next_due_at` 인덱스로 due한
행만 집는 구조로 가기 위한 계산 로직이다.

⚠️ **아직 읽기 경로가 아니다.** 문서(15장 4번)는 schedule 4종 count가 각각 활성 profile 수와
같고 중복 0인 것을 확인하기 전에는 tick의 full-profile scan을 제거하지 말라고 못박는다.
지금은 테이블을 채우고 대사만 한다.

시각은 기존 tick 상수를 **그대로** 쓴다. 여기서 값이 갈라지면 전환 시점에 제품 동작이 바뀐다.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

KIND_DAILY_DIGEST = "daily_digest"
KIND_DIARY_GENERATE = "diary_generate"
KIND_DIARY_MORNING = "diary_morning_notification"
KIND_EVENING_CHECKIN = "evening_checkin"

KINDS: tuple[str, ...] = (
    KIND_DAILY_DIGEST,
    KIND_DIARY_GENERATE,
    KIND_DIARY_MORNING,
    KIND_EVENING_CHECKIN,
)

# 로컬 시각. worker/tick.py·push_personalization.py의 상수와 같아야 한다 — 등가는 테스트로 고정한다.
LOCAL_HOUR: dict[str, int] = {
    KIND_DIARY_GENERATE: 4,    # tick.DIARY_HOUR
    KIND_DAILY_DIGEST: 5,      # push_personalization.GEN_HOUR
    KIND_DIARY_MORNING: 9,     # tick.MORNING_HOUR
    KIND_EVENING_CHECKIN: 20,  # tick.EVENING_HOUR
}


@dataclass(frozen=True, slots=True)
class Due:
    kind: str
    timezone_snapshot: str
    next_due_at: datetime


def next_due(kind: str, tz_name: str, now: datetime) -> datetime:
    """`now` 이후 처음 오는 해당 로컬 시각(UTC).

    DST로 그 시각이 존재하지 않는 날이 있다 — 그런 날은 건너뛰지 않고 다음 날로 민다.
    건너뛰면 그날 일기가 아예 안 나온다.
    """
    if kind not in LOCAL_HOUR:
        raise KeyError(f"알 수 없는 schedule 종류: {kind}")
    tz = ZoneInfo(tz_name)
    local = now.astimezone(tz)
    hour = LOCAL_HOUR[kind]
    candidate = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= local:
        candidate = candidate + timedelta(days=1)
    # DST 전이로 벽시계 시각이 어긋나면 다음 날로 민다(같은 시각이 두 번 오는 경우 포함).
    for _ in range(3):
        if candidate.astimezone(tz).hour == hour:
            break
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(ZoneInfo("UTC"))


def all_due(tz_name: str, now: datetime) -> list[Due]:
    """한 사용자의 4종 전부. profile 생성과 backfill이 같은 함수를 쓴다."""
    return [Due(k, tz_name, next_due(k, tz_name, now)) for k in KINDS]


_UPSERT = text("""
INSERT INTO user_schedules (user_id, kind, timezone_snapshot, next_due_at)
VALUES (:user_id, :kind, :tz, :due)
ON CONFLICT (user_id, kind) DO NOTHING
RETURNING id
""")


async def ensure_for_user(
    session: AsyncSession, user_id: uuid.UUID, *, timezone_name: str, now: datetime
) -> int:
    """4종을 멱등 생성한다. 반환 = 실제로 만든 행 수.

    profile 생성과 **같은 transaction**에서 불러야 한다 — 따로 만들면 그 사이에 죽은 사용자가
    schedule 없이 남아 영원히 스케줄되지 않는다.
    """
    made = 0
    for d in all_due(timezone_name, now):
        got = await session.execute(
            _UPSERT,
            {"user_id": user_id, "kind": d.kind, "tz": d.timezone_snapshot, "due": d.next_due_at},
        )
        made += got.first() is not None
    return made
