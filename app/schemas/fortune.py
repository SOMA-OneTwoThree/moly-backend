"""오늘의 운세 v3 API 계약."""

from __future__ import annotations

from datetime import date
import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from app.schemas.common import StrictResponse, UtcDatetime

Locale = Literal["ko", "en", "ja"]


def _normalize_locale_header(value: object) -> object:
    """지원하는 BCP 47 언어 태그를 응답용 기본 언어 코드로 좁힌다."""

    if not isinstance(value, str):
        return value
    matched = re.fullmatch(
        r"(ko|en|ja)(?:-[A-Za-z0-9]{2,8})*",
        value,
        flags=re.IGNORECASE,
    )
    return matched.group(1).lower() if matched else value


FortuneLocaleHeader = Annotated[Locale, BeforeValidator(_normalize_locale_header)]
Gender = Literal["man", "woman", "undisclosed"]
CategoryKey = Literal["love", "money", "work", "energy"]
LuckyColorKey = Literal[
    "red",
    "coral",
    "orange",
    "yellow",
    "green",
    "sky",
    "blue",
    "navy",
    "purple",
    "pink",
    "white",
    "beige",
]


class FortuneProfilePut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gender: Gender
    birth_date: date


class FortuneProfileView(StrictResponse):
    gender: Gender
    birth_date: date
    revision: int = Field(ge=1)


class FortuneProfileResponse(StrictResponse):
    profile: FortuneProfileView


class FortuneProfilePutResponse(FortuneProfileResponse):
    result_invalidated: bool
    unlock_preserved: bool


class FortuneBasicOverallResult(StrictResponse):
    score: int = Field(ge=0, le=100)
    headline: str = Field(min_length=1)
    do: str = Field(min_length=1)
    pause: str = Field(min_length=1)


class FortuneOverallResult(FortuneBasicOverallResult):
    flow: list[str] = Field(min_length=3, max_length=3)


class FortuneCategoryResult(StrictResponse):
    score: int = Field(ge=0, le=100)
    text: list[str] = Field(min_length=2, max_length=2)


class FortuneCategories(StrictResponse):
    love: FortuneCategoryResult
    money: FortuneCategoryResult
    work: FortuneCategoryResult
    energy: FortuneCategoryResult


class FortuneLuckyColor(StrictResponse):
    key: LuckyColorKey
    name: str = Field(min_length=1)
    hex: str = Field(pattern=r"^#[0-9A-F]{6}$")


class FortuneBasicResult(StrictResponse):
    schema_version: Literal[3] = 3
    locale: Locale
    overall: FortuneBasicOverallResult
    lucky_color: FortuneLuckyColor


class FortuneResult(FortuneBasicResult):
    overall: FortuneOverallResult
    categories: FortuneCategories


class FortuneVersions(StrictResponse):
    ephemeris: str
    rules: str
    copy_version: str = Field(alias="copy", serialization_alias="copy")


class DailyFortuneStatusResponse(StrictResponse):
    available: bool
    state: Literal["profile_required", "unseen", "locked", "revealed"] | None = None
    access: Literal["included", "ad_required", "unlocked_today"] | None = None
    local_date: date | None = None
    result: FortuneResult | FortuneBasicResult | None = None
    versions: FortuneVersions | None = None

    @model_validator(mode="after")
    def result_matches_state(self) -> "DailyFortuneStatusResponse":
        optional = (self.state, self.access, self.local_date, self.result, self.versions)
        if not self.available:
            if any(value is not None for value in optional):
                raise ValueError("unavailable status cannot include state or content")
            return self
        if self.state is None or self.access is None:
            raise ValueError("available status requires state and access")
        if self.state == "profile_required":
            if self.access == "unlocked_today":
                raise ValueError("profile_required cannot have a daily unlock")
            if any(value is not None for value in (self.local_date, self.result, self.versions)):
                raise ValueError("profile_required cannot include date or content")
            return self
        if self.local_date is None:
            raise ValueError("fortune state requires a local date")
        if self.state == "revealed":
            if self.access != "unlocked_today":
                raise ValueError("status only exposes revealed content after today's unlock")
            if not isinstance(self.result, FortuneResult) or self.versions is None:
                raise ValueError("revealed status requires result and versions")
        elif self.state == "locked":
            if self.access == "unlocked_today":
                raise ValueError("locked status cannot have a daily unlock")
            if type(self.result) is not FortuneBasicResult or self.versions is None:
                raise ValueError("locked status requires basic result and versions only")
        elif self.result is not None or self.versions is not None:
            raise ValueError("non-revealed status cannot include content")
        return self


class DailyFortuneRevealResponse(StrictResponse):
    state: Literal["locked", "revealed"]
    access: Literal["included", "ad_required", "unlocked_today"]
    local_date: date
    result: FortuneResult | FortuneBasicResult | None = None
    versions: FortuneVersions | None = None

    @model_validator(mode="after")
    def result_matches_state(self) -> "DailyFortuneRevealResponse":
        if self.state == "locked":
            if (
                self.access != "ad_required"
                or type(self.result) is not FortuneBasicResult
                or self.versions is None
            ):
                raise ValueError("locked response requires an ad, basic result, and versions only")
        elif (
            self.access == "ad_required"
            or not isinstance(self.result, FortuneResult)
            or self.versions is None
        ):
            raise ValueError("revealed response requires access, result, and versions")
        return self


class FortuneAdSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: UUID


class FortuneAdSessionResponse(StrictResponse):
    session_id: UUID
    custom_data: str
    admob_user_id: UUID
    expires_at: UtcDatetime
