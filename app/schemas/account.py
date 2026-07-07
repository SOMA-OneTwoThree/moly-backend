"""계정 요청 스키마. 닉네임 ≤10자(API_SPEC §2) — 위반 시 422 VALIDATION."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class OnboardingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 미정의 필드 거부(심층방어)

    nickname: str = Field(min_length=1, max_length=10)
    timezone: str = Field(min_length=1)  # IANA
    language: str = Field(min_length=2, max_length=8)  # ISO 639-1


class PatchMeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nickname: str | None = Field(default=None, min_length=1, max_length=10)
    language: str | None = Field(default=None, min_length=2, max_length=8)
    timezone: str | None = Field(default=None, min_length=1)
