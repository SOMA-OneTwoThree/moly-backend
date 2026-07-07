"""GET /app-config — 부팅 최초 호출(인증 불필요). 강제 업데이트·점검 게이트 + 렌더 설정."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.services.app_config import get_public_app_config

router = APIRouter(tags=["system"])


@router.get("/app-config")
async def app_config(cfg: dict[str, Any] = Depends(get_public_app_config)) -> dict[str, Any]:
    return cfg
