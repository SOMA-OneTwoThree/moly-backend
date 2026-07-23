"""공통 성공 응답 스키마."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, PlainSerializer

# 서비스가 내던 isoformat 와이어 포맷(+00:00) 유지 — pydantic 기본 json 직렬화는 'Z'로 바꾼다.
UtcDatetime = Annotated[
    datetime, PlainSerializer(lambda dt: dt.isoformat(), return_type=str, when_used="json")
]


class StrictResponse(BaseModel):
    """서비스 반환값과 공개 계약의 조용한 드리프트를 막는 응답 기반 클래스."""

    model_config = ConfigDict(extra="forbid")


class StatusResponse(StrictResponse):
    status: Literal["ok"]


class HealthResponse(StatusResponse):
    app: str
    env: str
    version: str  # 배포 커밋 sha(git_sha) — 배포 반영 확인용
