"""Read-only banner endpoint; the deploy image owns the catalog."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_day import validate_app_timezone
from app.core.db import get_session
from app.core.errors import AppError
from app.core.security import get_current_user
from app.schemas.banners import BannerFeed
from app.services import banners

router = APIRouter(tags=["banners"])
_log = logging.getLogger("moly-backend")
Capability = Annotated[str, Query(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


@router.get("/banners", response_model=BannerFeed, operation_id="listBanners")
async def list_banners(
    request: Request,
    placement: str,
    schema_version: Annotated[int, Query(gt=0)],
    platform: Literal["android", "ios"],
    app_version: Annotated[str, Query(min_length=1, max_length=64)],
    capabilities: Annotated[list[Capability], Query(max_length=32)],
    x_app_locale: Annotated[str | None, Header(max_length=64)] = None,
    x_app_timezone: Annotated[str | None, Header()] = None,
    user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BannerFeed:
    if placement != "home_blind":
        raise AppError("BANNER_PLACEMENT_UNSUPPORTED", 422, "지원하지 않는 배너 위치입니다.")
    if schema_version != 1:
        raise AppError("BANNER_SCHEMA_UNSUPPORTED", 422, "지원하지 않는 배너 버전입니다.")
    validate_app_timezone(x_app_timezone)
    catalog = getattr(request.app.state, "banner_catalog", None)
    if catalog is None:
        raise AppError("BANNERS_UNAVAILABLE", 503, "배너를 불러올 수 없습니다.")
    try:
        started = getattr(request.state, "started_monotonic", time.monotonic())
        budget = started + 2 - time.monotonic()
        if budget <= 0:
            raise TimeoutError("banner request budget exhausted")
        async with asyncio.timeout(budget):
            result = await banners.list_banners(
                catalog,
                session,
                user_id,
                now=datetime.now(timezone.utc),
                platform=platform,
                app_version=app_version,
                locale=x_app_locale,
                timezone_name=x_app_timezone,
                capabilities=frozenset(capabilities),
            )
            if time.monotonic() - started > 2:
                raise TimeoutError("banner compilation budget exhausted")
            return result
    except Exception as exc:
        _log.warning("banner request unavailable: %s", type(exc).__name__)
        raise AppError("BANNERS_UNAVAILABLE", 503, "배너를 불러올 수 없습니다.") from exc
