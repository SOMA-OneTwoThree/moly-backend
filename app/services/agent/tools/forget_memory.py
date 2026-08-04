"""최종 agent step 전용 망각 제어 도구. 런타임이 intent로 변환하며 직접 execute하지 않는다."""
from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.services.agent.tools.base import ToolArgs


class ForgetMemoryArgs(ToolArgs):
    target_fact_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    value: str | None = Field(default=None, max_length=100)
    future_learning: Literal["allow", "block"] = "allow"


class ForgetMemoryOut(BaseModel):
    accepted: bool


NAME = "forget_memory"
DESCRIPTION = (
    "Confirm a user's explicit request to forget memory facts. "
    "Use target_fact_ids returned by recall_memory, or a canonical predicate value. "
    "Set future_learning=block only when the user explicitly says never remember it again."
)
