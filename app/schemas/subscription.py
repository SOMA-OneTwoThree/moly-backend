"""구독 조회·플랜 응답 + RevenueCat 웹훅 요청 스키마.

RC 웹훅은 top-level 형태만 강제한다 — RC가 새 event type/field를 예고 없이 추가하므로
(공식 가이드) event 내부는 extra 허용, type presence만 필수. 위반은 422로 거절해
RC 대시보드에 실패로 노출시킨다.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import StrictResponse, UtcDatetime


class RevenueCatEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str = Field(min_length=1)


class RevenueCatWebhook(BaseModel):
    model_config = ConfigDict(extra="allow")

    api_version: str = Field(min_length=1)  # RC 공식 문서상 모든 웹훅에 존재(non-nullable)
    event: RevenueCatEvent


class SubscriptionResponse(StrictResponse):
    status: Literal["none", "active", "grace_period", "expired", "revoked"]
    plan: Literal["monthly", "yearly"] | None
    auto_renew_enabled: bool
    expires_at: UtcDatetime | None
    in_trial: bool
    trial_ends_at: UtcDatetime | None


class SubscriptionPlan(StrictResponse):
    product_id: str
    period: Literal["monthly", "yearly"]
    hay_grant: int = Field(ge=0)


class SubscriptionPlansResponse(StrictResponse):
    plans: list[SubscriptionPlan]
    benefits: list[str]
