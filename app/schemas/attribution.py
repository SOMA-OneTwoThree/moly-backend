"""Meta 설치 리퍼러 복호화 요청·응답 스키마."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import StrictResponse


class MetaReferrerDecryptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Google Play 설치 리퍼러의 utm_content 원문. 인증 없는 경로라 길이 상한을 둔다.
    utm_content: str = Field(max_length=8192)


class MetaAttribution(StrictResponse):
    """복호화된 귀속 값. Meta는 숫자처럼 보이는 ID도 전부 문자열로 준다."""

    ad_id: str | None = None
    adgroup_id: str | None = None
    adgroup_name: str | None = None
    campaign_id: str | None = None
    campaign_name: str | None = None
    campaign_group_id: str | None = None
    campaign_group_name: str | None = None
    account_id: str | None = None
    ad_objective_name: str | None = None


class MetaReferrerDecryptResponse(StrictResponse):
    attribution: MetaAttribution | None
