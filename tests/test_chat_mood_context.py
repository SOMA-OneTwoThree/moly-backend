"""Current mood freshness, compact temporal context, and bounded historical reads."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.time_utils import safe_zone
from app.services import chat, checkpoint, mood_context
from app.services.agent import runtime
from app.services.agent.tools.base import InvalidArguments
from app.services.agent.tools.get_mood_entries import GetMoodEntriesArgs, TOOL
from app.services.llm import ToolResult
from tests.test_chat import FakeSession
from tests.test_chat_turn_context_wiring import _post

NOW = datetime(2026, 9, 14, 15, 1, tzinfo=timezone.utc)  # 09/15 00:01 KST
TODAY = date(2026, 9, 15)
UID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _message(i, at, content="배불러", sender="user"):
    return SimpleNamespace(
        id=i, sender=sender, content=content, created_at=at, activity_date=date(2026, 9, 14),
    )


async def test_midnight_history_stays_fixed_when_time_and_mood_change(monkeypatch):
    history = [_message(2, NOW - timedelta(minutes=2), "그랬구나", "moly"),
               _message(1, NOW - timedelta(minutes=2))]
    earlier, _, _ = await chat._context(
        FakeSession(execute_items=history), UID, 0, current_text="많이 먹었나 봐",
        current_date=date(2026, 9, 14), current_at=NOW,
    )
    later, _, _ = await chat._context(
        FakeSession(execute_items=history), UID, 0, current_text="저녁 뭐 먹지?",
        current_date=TODAY, current_at=NOW + timedelta(hours=18),
    )
    assert earlier[:-1] == later[:-1]  # Prefix never gets relative age annotations.
    assert earlier[0]["content"].startswith("[2026-09-14T23:59+09:00]")
    assert earlier[1]["content"] == "그랬구나"  # Same-minute duplicate is omitted.
    assert earlier[-1]["content"].startswith("[2026-09-15T00:01+09:00]")
    assert later[-1]["content"].startswith("[2026-09-15T18:01+09:00]")


def test_missing_timestamp_is_not_replaced_by_activity_date():
    convo = [{"role": "user", "content": "배불렀어"}]
    chat._mark_dates(convo, [_message(1, None)])
    assert convo[0]["content"] == "[time unknown]\n배불렀어"


@pytest.mark.parametrize("zone,expected", [
    ("Asia/Seoul", "2026-09-15T00:01+09:00"),
    ("America/New_York", "2026-09-14T11:01-04:00"),
    ("broken/zone", "2026-09-15T00:01+09:00"),
])
def test_timestamps_use_calendar_time_and_explicit_offset(zone, expected):
    assert chat._time_label(NOW, zone) == f"[{expected}]"


def test_elapsed_time_uses_real_minutes_across_midnight_and_dst():
    assert "Minutes since previous turn: 2." in chat._time_context(
        NOW, NOW - timedelta(minutes=2), "Asia/Seoul",
    )
    # Fall-back repeats 01:30 in New York, but the instants are an hour apart.
    first = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    block = chat._time_context(first + timedelta(hours=1), first, "America/New_York")
    assert "01:30-05:00" in block and "01:30-04:00" in block
    assert "Minutes since previous turn: 60." in block
    unknown = chat._time_context(NOW, None, "Asia/Seoul")
    assert "last_turn=" not in unknown and "Minutes since" not in unknown


async def test_mood_changes_only_the_volatile_system_slot(monkeypatch):
    block = AsyncMock(side_effect=[
        '[User mood entry]\n{"status":"present","note":"아침에 속상했어"}',
        '[User mood entry]\n{"status":"absent"}',
    ])
    monkeypatch.setattr(mood_context, "today_block", block)
    before, after = {}, {}
    first_session = FakeSession()
    await _post(first_session, monkeypatch, capture=before)
    await _post(FakeSession(), monkeypatch, capture=after)
    assert before["system"] == after["system"]
    assert "아침에 속상했어" not in "\n".join(before["system"])
    assert before["convo"][-2]["role"] == "system"
    assert "아침에 속상했어" in before["convo"][-2]["content"]
    assert "아침에 속상했어" not in after["convo"][-2]["content"]
    assert "아침에 속상했어" not in before["convo"][-1]["content"]
    assert all("아침에 속상했어" not in (getattr(row, "content", "") or "")
               for row in first_session.added)
    assert block.await_count == 2


@pytest.mark.parametrize("args,expected", [
    ({"period": "today"}, (TODAY, TODAY)),
    ({"period": "yesterday"}, (TODAY - timedelta(days=1), TODAY - timedelta(days=1))),
    ({}, (TODAY - timedelta(days=6), TODAY)),
    ({"from": "2025-01-01"}, (date(2025, 1, 1), date(2025, 1, 1))),
    ({"to": "2025-01-01"}, (date(2025, 1, 1), date(2025, 1, 1))),
    ({"from": "2025-01-01", "to": "2025-01-31"}, (date(2025, 1, 1), date(2025, 1, 31))),
])
def test_mood_dates_are_exact_and_bounded(args, expected):
    assert GetMoodEntriesArgs.model_validate(args).dates(TODAY) == expected


@pytest.mark.parametrize("args", [
    {"period": "yesterday", "from": "2025-01-01"},
    {"from": "2025-01-02", "to": "2025-01-01"},
    {"from": "2025-01-01", "to": "2025-02-01"},
    {"to": "2026-09-16"},
])
def test_invalid_mood_ranges_are_rejected(args):
    with pytest.raises(InvalidArguments):
        GetMoodEntriesArgs.model_validate(args).dates(TODAY)


async def test_yesterday_missing_never_falls_back_to_recent_entries(monkeypatch):
    read = AsyncMock(return_value=[])
    monkeypatch.setattr(mood_context, "read_entries", read)
    ctx = runtime.ToolContext(UID, "ko", TODAY - timedelta(days=1), 0, TODAY)
    out = await TOOL.execute(ctx, {"period": "yesterday"}, None)
    assert out.status == "ok" and out.data["matched_count"] == 0
    assert out.data["items"] == []
    assert read.call_args.args[1:] == (UID, date(2026, 9, 14), date(2026, 9, 14))


@pytest.mark.parametrize("budget", [600, 300, 180, 1])
def test_tool_budget_preserves_dates_and_counts_and_does_not_mutate_raw_results(budget):
    data = {
        "from_date": "2026-08-16", "to_date": "2026-09-15", "matched_count": 31,
        "returned_count": 5, "has_more": True,
        "items": [{"date": f"2026-09-{15-i:02}", "kind": "tired", "note": "기분" * 100}
                  for i in range(5)],
    }
    raw = ToolResult(call_id="c1", tool_name=TOOL.name, status="ok", data=data)
    saved = json.dumps(data)
    out = runtime.apply_result_budget([raw], budget)[0]
    assert json.dumps(raw.data) == saved
    if out.status == "ok":
        assert runtime._cost(out) <= min(480, budget)
        assert out.data["from_date"] == "2026-08-16"
        assert out.data["to_date"] == "2026-09-15"
        assert out.data["matched_count"] == 31
        assert out.data["returned_count"] == len(out.data["items"])
        assert out.data["has_more"] is True
        for item in out.data["items"]:
            date.fromisoformat(item["date"])
    else:
        assert out.error_code == "budget_exceeded" and out.data is None


def test_checkpoint_time_affects_v4_hash_but_not_pending_v3_jobs():
    a = checkpoint.SourceMessage(1, "user", "normal", "배불러", NOW)
    b = checkpoint.SourceMessage(1, "user", "normal", "배불러", NOW + timedelta(hours=6))
    assert checkpoint.source_hash(previous=None, messages=[a]) != checkpoint.source_hash(
        previous=None, messages=[b],
    )
    legacy = checkpoint.LEGACY_SUMMARIZER_VERSION
    assert checkpoint.source_hash(previous=None, messages=[a], version=legacy) == (
        checkpoint.source_hash(previous=None, messages=[b], version=legacy)
    )
    assert "2026-09-14T15:01" in checkpoint.render_conversation([a])
    assert "2026-09-14" not in checkpoint.render_conversation([a], version=legacy)


async def test_today_lookup_failure_does_not_claim_no_record():
    # Missing begin_nested simulates failure before querying; the prompt reports unavailability.
    block = await mood_context.today_block(None, UID, TODAY, zone=safe_zone("Asia/Seoul"))
    assert json.loads(block.split("\n", 1)[1])["status"] == "unavailable"
