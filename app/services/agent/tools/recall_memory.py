"""정규화 사실과 검증된 원문 에피소드를 함께 회상하는 대화 도구."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import memory_embeddings, recall_memory
from app.services.agent.runtime import ToolContext
from app.services.agent.tools.base import BaseTool, InvalidArguments, ToolArgs


class RecallMemoryArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=200)
    need: Literal["summary", "exact", "quote"] = "summary"
    from_: dt.date | None = Field(default=None, alias="from")
    to: dt.date | None = None
    limit: int = Field(default=3, ge=1, le=5)


class RecallMemoryOut(BaseModel):
    status: str
    matched_count: int
    returned_count: int
    coverage: str
    has_more: bool
    items: list[dict]


class RecallMemoryTool(BaseTool):
    name = "recall_memory"
    description = (
        "Recall verified facts and exact past user episodes for natural questions about what the "
        "assistant knows or remembers."
    )
    input_model = RecallMemoryArgs
    output_model = RecallMemoryOut

    async def prepare(self, ctx: ToolContext, args: dict) -> list[float]:
        return await memory_embeddings.embed_query(str(args["query"]))

    async def run(self, ctx, args, session):  # pragma: no cover
        raise RuntimeError("recall_memory requires prepared execution")

    async def run_prepared(
        self,
        ctx: ToolContext,
        args: RecallMemoryArgs,
        session: AsyncSession,
        prepared: list[float],
    ) -> tuple[RecallMemoryOut, bool]:
        if args.from_ and args.to and args.from_ > args.to:
            raise InvalidArguments("from_after_to")
        result = await recall_memory.recall(
            session,
            ctx.user_id,
            query=args.query,
            need=args.need,
            from_date=args.from_,
            to_date=args.to,
            limit=args.limit,
            query_embedding=prepared,
        )
        return RecallMemoryOut.model_validate(result), False


TOOL = RecallMemoryTool()
