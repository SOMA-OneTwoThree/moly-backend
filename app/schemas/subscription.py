"""구독·IAP 요청 스키마. 상품·수량·플랜은 서버가 JWS에서 파생(클라 값 불신)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signed_transaction: str = Field(min_length=1)


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signed_transactions: list[str] = Field(min_length=1)


class IapPurchaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signed_transaction: str = Field(min_length=1)
