"""상점 요청 스키마. 장착은 4슬롯 전체 교체(null=해제)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PurchaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(min_length=1)


class EquipmentPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 4슬롯 모두 필수(전체 교체). null = 해제.
    background_id: str | None
    head_id: str | None
    neck_id: str | None
    body_id: str | None
