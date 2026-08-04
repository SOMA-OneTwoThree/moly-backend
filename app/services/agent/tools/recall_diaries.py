"""자연스러운 대화용 일기 회상 도구."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import memory_embeddings, recall_diaries
from app.services.agent.runtime import ToolContext
from app.services.agent.tools.base import BaseTool, InvalidArguments, ToolArgs


class RecallDiariesArgs(ToolArgs):
    query: str | None = Field(default=None, min_length=1, max_length=200)
    need: Literal["count", "summary", "full", "full_card", "quote"] = "summary"
    from_: dt.date | None = Field(default=None, alias="from")
    to: dt.date | None = None
    focus_id: uuid.UUID | None = None
    limit: int = Field(default=3, ge=1, le=5)


class RecallDiariesOut(BaseModel):
    status: str
    matched_count: int
    returned_count: int
    coverage: str
    has_more: bool
    items: list[dict]


class RecallDiariesTool(BaseTool):
    name = "recall_diaries"
    description = (
        "Recall the user's published diaries conversationally. Return counts, coverage, titles, "
        "dates, excerpts, and full bodies when needed in one call."
    )
    input_model = RecallDiariesArgs
    output_model = RecallDiariesOut

    async def prepare(self, ctx: ToolContext, args: dict) -> list[float] | None:
        query = args.get("query")
        return await memory_embeddings.embed_query(str(query)) if query else None

    async def run(self, ctx, args, session):  # pragma: no cover
        raise RuntimeError("recall_diaries requires prepared execution")

    async def run_prepared(
        self,
        ctx: ToolContext,
        args: RecallDiariesArgs,
        session: AsyncSession,
        prepared: list[float] | None,
    ) -> tuple[RecallDiariesOut, bool]:
        if args.from_ and args.to and args.from_ > args.to:
            raise InvalidArguments("from_after_to")
        result = await recall_diaries.recall(
            session,
            ctx.user_id,
            query=args.query,
            need=args.need,
            from_date=args.from_,
            to_date=args.to,
            focus_id=args.focus_id,
            limit=args.limit,
            query_embedding=prepared,
        )
        return RecallDiariesOut.model_validate(result), result["coverage"] == "partial"


TOOL = RecallDiariesTool()
