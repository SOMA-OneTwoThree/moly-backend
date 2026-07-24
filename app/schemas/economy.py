"""지갑·충전소 성공 응답 스키마."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import StrictResponse, UtcDatetime

TransactionType = Literal[
    "attendance",
    "ad_reward",
    "routine_reward",
    "iap_purchase",
    "subscription_grant",
    "shop_purchase",
    "refund_revoke",
    "admin_adjustment",
]


class WalletResponse(StrictResponse):
    balance: int = Field(ge=0)


class HayTransactionResponse(StrictResponse):
    id: str = Field(pattern=r"^\d+$")
    type: TransactionType
    amount: int
    # DB에 balance_after >= 0 CHECK가 없다 — 레거시 음수 행이 조회를 500으로 막으면 안 된다.
    balance_after: int
    created_at: UtcDatetime

    @model_validator(mode="after")
    def amount_must_be_nonzero(self) -> "HayTransactionResponse":
        if self.amount == 0:
            raise ValueError("원장 거래 금액은 0일 수 없습니다.")
        return self


class TransactionsResponse(StrictResponse):
    data: list[HayTransactionResponse]
    next_cursor: str | None = Field(default=None, pattern=r"^\d+$")


class AttendanceStatus(StrictResponse):
    claimable: bool
    claimed: bool
    reward: int = Field(ge=0)


class AdStatus(StrictResponse):
    views_used: int = Field(ge=0)
    views_limit: int = Field(ge=0)
    reward_per_view: int = Field(ge=0)


class RoutinePairStatus(StrictResponse):
    completed_today: int = Field(ge=0)
    required: int = Field(ge=0)
    claimable: bool
    claimed: bool
    reward: int = Field(ge=0)


class HayProduct(StrictResponse):
    product_id: str  # App Store 상품ID(RC SDK 구매에 사용)
    play_store_product_id: str | None = None  # Google Play 상품ID(미확정 시 null)
    amount: int


class ChargingStationResponse(StrictResponse):
    # 이름은 레거시지만 값의 의미는 00:00 경계 reward_date다.
    activity_date: date
    attendance: AttendanceStatus
    ad: AdStatus
    routine_pair: RoutinePairStatus
    hay_products: list[HayProduct]
    balance: int = Field(ge=0)


class RewardResponse(StrictResponse):
    granted: int = Field(ge=0)
    balance_after: int = Field(ge=0)
