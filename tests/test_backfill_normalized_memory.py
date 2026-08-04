"""과거 메시지 → 정규화 source turn 백필의 결정성과 재실행 안전성."""
from datetime import date, datetime, timezone

from app.models.message import Message
from scripts.backfill_normalized_memory import _turns


def _message(message_id: int, sender: str) -> Message:
    item = Message(
        sender=sender,
        content=str(message_id),
        activity_date=date(2026, 8, 4),
        created_at=datetime(2026, 8, 4, message_id, tzinfo=timezone.utc),
    )
    item.id = message_id
    return item


def test_turns_group_each_inbound_with_following_assistant_messages():
    turns = _turns([
        _message(1, "user"), _message(2, "moly"),
        _message(3, "user"), _message(4, "moly"), _message(5, "moly"),
    ], set())
    assert [(t.representative_message_id, t.message_ids) for t in turns] == [
        (1, (1, 2)), (3, (3, 4, 5)),
    ]


def test_turns_skip_already_mapped_inbound_on_replay():
    turns = _turns([
        _message(1, "user"), _message(2, "moly"),
        _message(3, "user"), _message(4, "moly"),
    ], {1, 2})
    assert [(t.representative_message_id, t.message_ids) for t in turns] == [(3, (3, 4))]
