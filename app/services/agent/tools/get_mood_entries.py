"""Calendar-date reads of the user's own mood journal, separate from Cappy's diaries."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from app.services import mood_context
from app.services.agent.tools.base import BaseTool, InvalidArguments, ToolArgs


class GetMoodEntriesArgs(ToolArgs):
    period: Literal["today", "yesterday", "last_7_days"] | None = Field(
        default=None, description="Relative calendar dates. Omit when using from/to.",
    )
    from_: date | None = Field(default=None, alias="from", description="First date, inclusive.")
    to: date | None = Field(default=None, description="Last date, inclusive. One date means one day.")
    limit: int = Field(
        default=3, ge=1, le=5,
        description="Entries to return, at most 5 even for longer periods.",
    )

    def dates(self, today: date) -> tuple[date, date]:
        if self.period is not None and (self.from_ is not None or self.to is not None):
            raise InvalidArguments("mixed_date_selectors")
        if self.from_ is not None or self.to is not None:
            start, end = self.from_ or self.to, self.to or self.from_
        elif self.period == "today":
            start = end = today
        elif self.period == "yesterday":
            start = end = today - timedelta(days=1)
        else:
            start, end = today - timedelta(days=6), today
        if end > today or not 0 <= (end - start).days < 31:
            raise InvalidArguments("date_out_of_range")
        return start, end


class MoodItem(BaseModel):
    date: str
    kind: str
    note: str


class GetMoodEntriesOut(BaseModel):
    from_date: str
    to_date: str
    matched_count: int
    returned_count: int
    has_more: bool
    items: list[MoodItem]


class GetMoodEntriesTool(BaseTool):
    name = "get_mood_entries"
    description = (
        "Read the user's own mood journal (feeling and note), not Cappy's diaries. "
        "Only when the user asks to look up or compare their mood records. "
        "Never call proactively or infer a journal topic from a vague feeling. "
        "Today's entry is already provided; do not query it again when available. Defaults to the last 7 days. "
        "Use period for today/yesterday, or exact from/to (up to 31 days, including older dates). "
        "Return at most 5 excerpts; limit must not exceed 5. Missing dates stay missing. "
        "Results may be partial; never infer a whole period from excerpts."
    )
    input_model = GetMoodEntriesArgs
    output_model = GetMoodEntriesOut

    async def run(self, ctx, args, session):
        if ctx.local_calendar_date is None:
            raise InvalidArguments("calendar_date_required")
        start, end = args.dates(ctx.local_calendar_date)
        rows = await mood_context.read_entries(session, ctx.user_id, start, end)
        items, truncated = [], False
        for row in rows[:args.limit]:
            note, cut = mood_context.clipped(row["note"], 200)
            truncated = truncated or cut or row["truncated"]
            items.append(MoodItem(date=row["date"], kind=row["kind"], note=note))
        return GetMoodEntriesOut(
            from_date=start.isoformat(), to_date=end.isoformat(), matched_count=len(rows),
            returned_count=len(items), has_more=len(rows) > len(items), items=items,
        ), truncated


TOOL = GetMoodEntriesTool()
