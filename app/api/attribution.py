"""설치 귀속 API — Meta 설치 리퍼러 복호화. 공개(설치 직후, 로그인 전에 호출된다)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.schemas.attribution import MetaReferrerDecryptRequest, MetaReferrerDecryptResponse
from app.services import attribution

router = APIRouter(tags=["attribution"])


@router.post(
    "/attribution/meta-referrer/decrypt",
    response_model=MetaReferrerDecryptResponse,
)
async def decrypt_meta_referrer(req: MetaReferrerDecryptRequest) -> dict[str, Any]:
    """utm_content를 복호화. 암호문이 없으면 attribution=null(200), 복호화 실패 422, 키 미설정 503."""
    return {"attribution": attribution.decrypt_meta_referrer(req.utm_content)}
