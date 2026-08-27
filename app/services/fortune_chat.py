"""오늘 운세 결과를 한 요청의 휘발 컨텍스트로만 전달한다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import errors
from app.models.fortune import DailyFortune, FortuneProfile
from app.services import fortune, fortune_catalog


@dataclass(frozen=True, slots=True)
class FortuneContextSnapshot:
    local_date: date
    locale: str
    fingerprint: str
    block: str


def _stale() -> errors.AppError:
    return errors.AppError(
        "FORTUNE_CONTEXT_STALE",
        409,
        "오늘의 운세가 바뀌었어요. 결과 화면에서 다시 시작해 주세요.",
    )


def _render(row: DailyFortune, locale: str) -> str:
    result = fortune._public_result(row, locale)
    copy = {
        "ko": {
            "header": "서버가 확인한 오늘의 운세 데이터",
            "fortune": "오늘의 운세",
            "index": "행운 지수",
            "do": "오늘 해볼 것",
            "pause": "오늘 조심할 것",
            "color": "행운색",
            "categories": {"love": "애정", "money": "금전", "work": "일", "energy": "활력"},
        },
        "ja": {
            "header": "サーバーで確認済みの今日の運勢データ",
            "fortune": "今日の運勢",
            "index": "運勢スコア",
            "do": "今日やってみること",
            "pause": "今日気をつけること",
            "color": "ラッキーカラー",
            "categories": {"love": "恋愛", "money": "金運", "work": "仕事", "energy": "健康"},
        },
        "en": {
            "header": "Today's verified fortune data",
            "fortune": "Today's outlook",
            "index": "Fortune score",
            "do": "Try today",
            "pause": "Watch out for",
            "color": "Lucky color",
            "categories": {"love": "Love", "money": "Money", "work": "Work", "energy": "Energy"},
        },
    }[result["locale"]]
    category_names = copy["categories"]
    categories = ", ".join(
        f"{category_names[key]} {result['categories'][key]['score']}"
        for key in ("love", "money", "work", "energy")
    )
    overall = result["overall"]
    return (
        f"[{copy['header']}]\n"
        f"{copy['fortune']}: {overall['headline']}\n"
        f"{copy['index']}: {overall['score']}/100\n"
        f"{' '.join(overall['flow'])}\n{categories}\n"
        f"{copy['do']}: {overall['do']}\n{copy['pause']}: {overall['pause']}\n"
        f"{copy['color']}: {result['lucky_color']['name']}"
    )


async def load_snapshot(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    local_date: date,
    locale: str,
    account_timezone: str,
) -> FortuneContextSnapshot:
    if not fortune._ready() or not settings.fortune_chat_enabled:
        raise errors.AppError("FEATURE_UNAVAILABLE", 403, "운세 대화를 사용할 수 없어요.")
    if locale not in fortune_catalog.SUPPORTED_LOCALES or local_date is None:
        raise _stale()
    profile = await session.get(FortuneProfile, user_id)
    row = await session.get(DailyFortune, user_id)
    if (
        profile is None
        or row is None
        or not fortune._current_row(
            row,
            today=local_date,
            timezone_name=account_timezone,
            revision=profile.revision,
        )
        or row.unlock_state != "unlocked"
        or row.revealed_at is None
        or locale not in row.copy_by_locale
    ):
        raise _stale()
    return FortuneContextSnapshot(
        local_date=local_date,
        locale=locale,
        fingerprint=fortune.result_fingerprint(row, locale),
        block=_render(row, locale),
    )


async def revalidate(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    snapshot: FortuneContextSnapshot,
    current_local_date: date,
    account_timezone: str,
) -> None:
    if not fortune._ready() or not settings.fortune_chat_enabled:
        raise errors.AppError("FEATURE_UNAVAILABLE", 403, "운세 대화를 사용할 수 없어요.")
    if current_local_date != snapshot.local_date:
        raise _stale()
    profile = await session.get(FortuneProfile, user_id, populate_existing=True)
    row = await session.get(DailyFortune, user_id, populate_existing=True)
    if (
        profile is None
        or row is None
        or not fortune._current_row(
            row,
            today=current_local_date,
            timezone_name=account_timezone,
            revision=profile.revision,
        )
        or row.unlock_state != "unlocked"
        or fortune.result_fingerprint(row, snapshot.locale) != snapshot.fingerprint
    ):
        raise _stale()
