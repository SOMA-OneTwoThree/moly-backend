"""로컬 전용 개발 API — 워커 배치(04:00 일기 생성)를 Swagger에서 손으로 돌린다.

⚠️ main.create_app()이 environment == "local"일 때만 등록한다. 프로덕션엔 라우트 자체가 없다.
   일기 생성은 원래 배치 전용이라 API가 없다. 튜닝 루프를 돌리려면 두 벽을 넘어야 한다:
   - 멱등: 같은 (user, diary_date) 행이 있으면 조용히 스킵 → force로 지우고 재생성
   - 발행시각: published_at = 익일 09:00이라 조회 API에 안 뜸 → publish_now로 현재 시각으로
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.db import get_session
from app.core.security import get_current_user
from app.core.time_utils import activity_date_for
from app.models.diary import Diary
from app.models.profile import Profile
from app.schemas.dev import DiaryGenerateRequest
from app.services import diary_generation
from app.services.account import _uid
from app.services.limits import effective_token_config

router = APIRouter(tags=["dev"], prefix="/dev")


@router.post("/diaries/generate")
async def generate_diary(
    req: DiaryGenerateRequest,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """워커의 일기 생성 1건을 지금 실행하고, 결과 + 왜 그렇게 나왔는지를 돌려준다."""
    uid = _uid(user_id)
    profile = await session.get(Profile, uid)
    if profile is None:
        raise errors.AppError("NOT_FOUND", 404, "프로필을 찾을 수 없어요.")

    now = datetime.now(timezone.utc)
    target = req.target_date or activity_date_for(now, profile.timezone)

    if req.force:
        await session.execute(
            delete(Diary).where(Diary.user_id == uid, Diary.diary_date == target)
        )
        await session.commit()

    cfg = await effective_token_config(session)
    diag = await diary_generation.generate_for_user(session, profile, target, cfg)

    diary = (
        await session.execute(
            select(Diary).where(Diary.user_id == uid, Diary.diary_date == target)
        )
    ).scalars().first()

    if diary is not None and req.publish_now:
        diary.published_at = now
        await session.commit()

    return {
        "target_date": target.isoformat(),
        # 왜 개인일기가 됐는지 / 왜 preset으로 빠졌는지
        "diagnostics": {
            **diag,
            "hint": _hint(diag),
        },
        "diary": None
        if diary is None
        else {
            "id": str(diary.id),
            "source": diary.source,  # llm = 개인일기 / preset = 캐피 자기일기
            "weather": diary.weather,
            "content": diary.content,
            "published_at": diary.published_at.isoformat() if diary.published_at else None,
        },
    }


def _hint(diag: dict[str, Any]) -> str:
    if diag.get("skipped"):
        return "이미 일기가 있어 스킵됨. force=true로 재생성하세요."
    if diag.get("source") == "llm":
        return "개인일기 생성 성공."
    if not diag.get("gate_passed"):
        return (
            f"게이트 미달 → preset 폴백. 당일 유저 메시지 {diag.get('user_chars')}자 "
            f"(필요 {diag.get('gate')}자). 대화를 더 하세요."
        )
    if diag.get("empty_body"):
        return "일기 LLM이 빈 본문을 반환 → preset 폴백."
    if diag.get("self_check_passed") is False:
        return "self-check(Haiku)가 환각으로 판정 → 개인일기 폐기, preset 폴백."
    return "preset 폴백."
