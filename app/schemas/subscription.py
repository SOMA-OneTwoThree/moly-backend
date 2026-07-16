"""구독 조회·플랜 응답 스키마."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.common import StrictResponse, UtcDatetime


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
