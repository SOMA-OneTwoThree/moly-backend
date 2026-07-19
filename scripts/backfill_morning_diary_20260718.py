"""7/18 활동 유저 중 웰컴-일기 날짜 충돌로 아침 일기가 스킵된 유저 백필(1회성).

배경: 웰컴 일기는 diary_date=가입일-1로 박히는데, 자정~04:00(로컬 경계) 사이 가입자는
그 날짜가 활동일(7/18)과 겹친다. 4시 배치는 unique(user,diary_date) 슬롯이 차 있으면
스킵하므로 대화 기반 아침 일기가 생성되지 않는다.

처리: 웰컴 일기를 하루 앞(가입일-2)으로 이동해 7/18 슬롯을 비우고, 그 유저에 대해
diary_generation.generate_for_user(target=7/18)을 호출한다.

기본은 dry-run. 실제 쓰기는 --apply.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select, text

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.core.db import get_sessionmaker  # noqa: E402
from app.models.profile import Profile  # noqa: E402
from app.services import diary_generation  # noqa: E402
from app.services.limits import effective_token_config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
_log = logging.getLogger("backfill")

TARGET = date(2026, 7, 18)  # 생성할 아침 일기의 활동일(=diary_date)

AFFECTED_SQL = text(
    """
    SELECT p.id
    FROM public.profiles p
    WHERE p.created_at >= '2026-07-18 11:00:00+00'  -- 7/18 20:00 KST 이후 가입
      AND EXISTS (SELECT 1 FROM public.messages m
                  WHERE m.user_id=p.id AND m.activity_date = :tgt AND m.kind='normal')
      AND EXISTS (SELECT 1 FROM public.diaries d
                  WHERE d.user_id=p.id AND d.diary_date = :tgt AND d.source='welcome')
      AND NOT EXISTS (SELECT 1 FROM public.diaries d
                      WHERE d.user_id=p.id AND d.diary_date = :tgt AND d.source IN ('llm','preset'))
    ORDER BY p.created_at
    """
)


async def _move_welcome(session, user_id, frm: date, to: date) -> bool:
    """웰컴 일기를 frm→to로 이동. to 슬롯이 이미 차 있으면 False(스킵)."""
    occupied = await session.execute(
        text("SELECT 1 FROM public.diaries WHERE user_id=:u AND diary_date=:to LIMIT 1"),
        {"u": user_id, "to": to},
    )
    if occupied.first() is not None:
        return False
    await session.execute(
        text(
            "UPDATE public.diaries SET diary_date=:to "
            "WHERE user_id=:u AND diary_date=:frm AND source='welcome'"
        ),
        {"u": user_id, "frm": frm, "to": to},
    )
    await session.commit()
    return True


async def main(apply: bool) -> None:
    if apply and not settings.anthropic_api_key:
        raise SystemExit(
            "ANTHROPIC_API_KEY가 설정되지 않았습니다. .env에 키를 넣고 다시 실행하세요."
        )

    async with get_sessionmaker()() as session:
        cfg = await effective_token_config(session)
        gate = cfg["diary_min_user_chars"]
        ids = [r[0] for r in (await session.execute(AFFECTED_SQL, {"tgt": TARGET})).all()]
        _log.info("대상 유저: %d명 (target diary_date=%s, gate=%s자)", len(ids), TARGET, gate)

        if not apply:
            _log.info("[DRY-RUN] --apply 없이는 아무것도 쓰지 않습니다.")
            _log.info("[DRY-RUN] 각 유저: 웰컴 %s→%s 이동 후 개인/preset 일기 생성 예정.",
                      TARGET, TARGET - timedelta(days=1))
            for uid in ids:
                _log.info("  - %s", uid)
            return

        made = {"llm": 0, "preset": 0, "skipped": 0, "failed": 0}
        for uid in ids:
            profile = (
                await session.execute(select(Profile).where(Profile.id == uid))
            ).scalars().first()
            if profile is None:
                made["skipped"] += 1
                continue
            new_welcome_date = TARGET - timedelta(days=1)
            moved = await _move_welcome(session, uid, TARGET, new_welcome_date)
            if not moved:
                _log.warning("welcome 이동 실패(대상 슬롯 점유) user=%s → 스킵", uid)
                made["skipped"] += 1
                continue
            try:
                res = await diary_generation.generate_for_user(session, profile, TARGET, cfg)
                src = res.get("source")
                made[src] = made.get(src, 0) + 1
                _log.info("생성 완료 user=%s source=%s chars=%s", uid, src, res.get("user_chars"))
            except Exception as e:  # noqa: BLE001 — 실패 시 웰컴 원복
                await session.rollback()
                await session.execute(
                    text(
                        "UPDATE public.diaries SET diary_date=:back "
                        "WHERE user_id=:u AND diary_date=:cur AND source='welcome'"
                    ),
                    {"u": uid, "cur": new_welcome_date, "back": TARGET},
                )
                await session.commit()
                made["failed"] += 1
                _log.error("생성 실패 user=%s (웰컴 원복함): %r", uid, e)

        _log.info("결과: %s", made)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="실제 DB 쓰기(미지정=dry-run)")
    args = ap.parse_args()
    asyncio.run(main(args.apply))
