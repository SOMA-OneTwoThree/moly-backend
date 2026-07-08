"""계정 요청 스키마. 닉네임 ≤10자(API_SPEC §2) — 위반 시 422 VALIDATION."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class OnboardingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 미정의 필드 거부(심층방어)

    nickname: str = Field(min_length=1, max_length=10)
    timezone: str = Field(min_length=1)  # IANA
    # ISO 639-1(+선택 지역). 문자·하이픈만 — 시스템 프롬프트에 삽입되므로 주입 문자 차단.
    language: str = Field(min_length=2, max_length=8, pattern=r"^[a-zA-Z]{2}(-[a-zA-Z]{2,4})?$")


class PatchMeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nickname: str | None = Field(default=None, min_length=1, max_length=10)
    language: str | None = Field(
        default=None, min_length=2, max_length=8, pattern=r"^[a-zA-Z]{2}(-[a-zA-Z]{2,4})?$"
    )
    timezone: str | None = Field(default=None, min_length=1)


class NotificationsPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 둘 다 선택 — 보낸 것만 반영. 알림 2종 고정(morning_diary·evening_chat).
    morning_diary: bool | None = None
    evening_chat: bool | None = None


class PushTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1)
    platform: str = Field(default="ios", min_length=1, max_length=16)


class LogoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    push_token: str = Field(min_length=1)
