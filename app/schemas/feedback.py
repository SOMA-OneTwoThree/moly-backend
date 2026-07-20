"""문의 요청 스키마. message 필수, contact(연락처)는 선택."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CreateFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    contact: str | None = Field(default=None, max_length=200)  # 이메일·전화·인스타 등 자유 입력
