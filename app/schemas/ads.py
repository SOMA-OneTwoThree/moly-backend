"""광고 보상 수령 요청."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AdRewardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ssv_transaction_id: str = Field(min_length=1)
