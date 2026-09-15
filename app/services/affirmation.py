"""오늘의 글귀 — 현지 날짜로 고르는 공용 문장과 사용자별 확인 상태.

문장 선택은 DB·배치·LLM 없이 현지 날짜 서수로 계산하므로 같은 날짜에는 모든 사용자가
같은 문장을 본다(주기별 고정 순열, `daily_affirmation` 참고). 사용자별로 저장하는 값은
`user_daily_stats.affirmation_acknowledged_at` 하나이며 배너 당일 숨김 판정에 쓴다.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.advisory_lock import advisory_xact_lock
from app.core.app_day import AppDay, validate_app_timezone
from app.core.errors import AppError
from app.models.user_daily_stats import UserDailyStats
from app.services import privacy
from app.services.account import _load_profile, _uid
from app.services.economy import _daily
from app.services.i18n import resolve

CATALOG_PATH = Path(__file__).resolve().parents[1] / "resources/affirmations.json"
Sentence = Annotated[str, Field(min_length=1, max_length=200)]


class AffirmationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AffirmationText(AffirmationModel):
    ko: Sentence
    en: Sentence
    ja: Sentence

    @field_validator("ko", "en", "ja")
    @classmethod
    def plain_sentence(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("affirmation text must be trimmed and non-blank")
        return value

    def for_locale(self, locale: str) -> str:
        return getattr(self, locale if locale in {"ko", "en", "ja"} else "en")


class AffirmationItem(AffirmationModel):
    id: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,64}$")]
    text: AffirmationText


class AffirmationManifest(AffirmationModel):
    format_version: Literal[1]
    items: Annotated[tuple[AffirmationItem, ...], Field(min_length=1, max_length=365)]

    @model_validator(mode="after")
    def unique_items(self):
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("duplicate affirmation id")
        return self


def load_manifest(raw: bytes) -> AffirmationManifest:
    """strict 검증 — 잘못된 카탈로그는 조용히 넘어가지 않고 즉시 실패한다."""
    return AffirmationManifest.model_validate_json(raw)


@lru_cache(maxsize=1)
def load_catalog(path: Path = CATALOG_PATH) -> AffirmationManifest:
    return load_manifest(path.read_bytes())


def _shuffled(cycle: int, items: tuple[AffirmationItem, ...]) -> list[AffirmationItem]:
    """주기별 고정 순열 — 같은 주기·같은 카탈로그면 언제나 같은 순서다."""
    return sorted(items, key=lambda item: hashlib.sha256(
        f"daily-affirmation-v1:{cycle}:{item.id}".encode()
    ).digest())


def _cycle_order(cycle: int, items: tuple[AffirmationItem, ...]) -> list[AffirmationItem]:
    order = _shuffled(cycle, items)
    # 주기 경계에서 같은 문장이 이틀 연속 나오지 않게 앞 두 개를 바꾼다. 3개 이상이면 이 교환이
    # 마지막 항목을 건드리지 않으므로 이전 주기의 끝을 순열만으로 판정할 수 있다. 2개 이하는
    # 어떤 순열로도 경계 반복을 없앨 수 없어 교환하지 않는다.
    if len(items) > 2 and order[0].id == _shuffled(cycle - 1, items)[-1].id:
        order[0], order[1] = order[1], order[0]
    return order


def daily_affirmation(local_date: date) -> AffirmationItem:
    """같은 현지 날짜에는 사용자·언어·재시작과 무관하게 같은 문장을 고른다.

    날짜를 카탈로그 크기로 나눠 주기와 위치를 얻고, 주기마다 고정된 순열에서 그 위치를 읽는다.
    한 주기(=문장 수) 안에서 모든 문장이 정확히 한 번씩 나오므로 근접 반복이 없다.
    """
    items = load_catalog().items
    cycle, position = divmod(local_date.toordinal(), len(items))
    return _cycle_order(cycle, items)[position]


async def _day(
    session: AsyncSession, user_id: str, timezone_name: str | None, now: datetime
) -> AppDay:
    validate_app_timezone(timezone_name)
    if timezone_name is None:
        timezone_name = (await _load_profile(session, user_id)).timezone
    return AppDay.at(now, timezone_name)


async def _acknowledged_at(
    session: AsyncSession, uid: uuid.UUID, local_date: date
) -> datetime | None:
    """읽기 전용 — 조회만으로 user_daily_stats 행을 만들지 않는다."""
    return (
        await session.execute(
            select(UserDailyStats.affirmation_acknowledged_at).where(
                UserDailyStats.user_id == uid,
                UserDailyStats.activity_date == local_date,
            )
        )
    ).scalar()


async def status(
    session: AsyncSession,
    user_id: str,
    *,
    locale: str | None,
    timezone_name: str | None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    uid = _uid(user_id)
    await privacy.ensure_subject_active(session, uid)
    day = await _day(session, user_id, timezone_name, now_utc or datetime.now(timezone.utc))
    item = daily_affirmation(day.local_date)
    language = resolve(locale)
    return {
        "local_date": day.local_date,
        "locale": language,
        "affirmation": {"id": item.id, "text": item.text.for_locale(language)},
        "acknowledged": await _acknowledged_at(session, uid, day.local_date) is not None,
    }


async def acknowledge(
    session: AsyncSession,
    user_id: str,
    *,
    local_date: date,
    timezone_name: str | None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    uid = _uid(user_id)
    now = now_utc or datetime.now(timezone.utc)
    day = await _day(session, user_id, timezone_name, now)
    if local_date != day.local_date:
        # 현지 날짜가 바뀐 뒤 옛 팝업이 보낸 확인은 오늘 카드를 숨기지 않는다.
        raise AppError(
            "DAILY_AFFIRMATION_STALE", 409, "날짜가 바뀌었어요. 오늘의 글귀를 다시 확인해 주세요."
        )
    # lock → 장벽 확인 → _daily → mutation → commit. 삭제 장벽 판정을 락 뒤에서 읽어야
    # 탈퇴 진행과 동시 요청이 엇갈리지 않는다(topic_entries·diary_generation과 같은 순서).
    await advisory_xact_lock(session, uid)
    await privacy.ensure_subject_active(session, uid)
    stats = await _daily(session, uid, day.local_date)
    if stats.affirmation_acknowledged_at is None:  # 재호출도 200(멱등)
        stats.affirmation_acknowledged_at = now
    await session.commit()
    return {"local_date": day.local_date, "acknowledged": True}
