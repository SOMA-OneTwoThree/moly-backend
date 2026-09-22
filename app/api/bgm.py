from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.bgm import BgmTracksResponse
from app.services.bgm import list_tracks

router = APIRouter(tags=["bgm"])


@router.get("/bgm/tracks", response_model=BgmTracksResponse)
async def tracks(
    response: Response,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    x_app_locale: str | None = Header(default=None, alias="X-App-Locale"),
) -> BgmTracksResponse:
    response.headers["Cache-Control"] = "private, no-store"
    return await list_tracks(session, x_app_locale)
