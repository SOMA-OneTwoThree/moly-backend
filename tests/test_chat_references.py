"""대화 일기 카드 capability와 서버 소유 focus 계약."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from app.models.conversational_recall import ChatResponseReference
from app.models.diary import Diary
from app.services import chat_references


UID = uuid.UUID("11111111-1111-1111-1111-111111111111")
DID = uuid.UUID("22222222-2222-2222-2222-222222222222")
RID = uuid.UUID("33333333-3333-3333-3333-333333333333")


class _Scalars:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _Result:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return _Scalars(self.values)


class _Session:
    def __init__(self, diary, *, counts=()):
        self.diary = diary
        self.counts = list(counts)
        self.added = []
        self.statements = []

    async def execute(self, stmt, params=None):
        self.statements.append(stmt)
        # 첫 실행은 eligible diary SELECT, 그 뒤는 focus UPSERT다.
        if len(self.statements) == 1:
            return _Result([self.diary])
        return _Result([])

    async def scalar(self, stmt, params=None):
        self.statements.append(stmt)
        return self.counts.pop(0)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        for value in self.added:
            if isinstance(value, ChatResponseReference) and value.id is None:
                value.id = RID
                value.schema_version = "diary-reference-v1"
                value.state = "available"


class _Ref:
    ref_type = "diary"
    ref_id = str(DID)


def _diary() -> Diary:
    return Diary(
        id=DID,
        user_id=UID,
        diary_date=date(2026, 8, 4),
        display_date=date(2026, 8, 4),
        kind="welcome",
        author="capi",
        source="welcome",
        title="첫 만남",
        content="오늘 처음 대화를 나눴다.",
        weather="sunny",
        record_status="published",
        published_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )


async def test_focus_persists_even_when_client_does_not_support_cards() -> None:
    session = _Session(_diary())
    wire = await chat_references.persist_selected(
        session,
        user_id=UID,
        reply_message_id=10,
        selected_refs=[_Ref()],
        response_mode="summary",
        focus_ref=_Ref(),
        nickname=None,
        context_revision=4,
        turn_seq=7,
        capability_enabled=False,
    )
    assert wire == []
    assert not session.added
    assert len(session.statements) == 2


async def test_full_card_is_db_rendered_only_for_capable_client() -> None:
    session = _Session(_diary())
    wire = await chat_references.persist_selected(
        session,
        user_id=UID,
        reply_message_id=10,
        selected_refs=[_Ref()],
        response_mode="full_card",
        focus_ref=_Ref(),
        nickname=None,
        context_revision=4,
        turn_seq=7,
        capability_enabled=True,
    )
    assert wire[0]["schema"] == "diary-reference-v1"
    assert wire[0]["diary"]["body"] == "오늘 처음 대화를 나눴다."
    assert wire[0]["diary"]["id"] == str(DID)
    assert len(session.added) == 1


async def test_phase_b_revalidates_every_selected_reference() -> None:
    valid = await chat_references.validate_selected(
        _Session(_diary(), counts=[1]),
        user_id=UID,
        selected_refs=[_Ref()],
        focus_ref=None,
    )
    invalid = await chat_references.validate_selected(
        _Session(_diary(), counts=[0]),
        user_id=UID,
        selected_refs=[_Ref()],
        focus_ref=None,
    )
    assert valid
    assert not invalid


async def test_unknown_grounding_type_fails_closed() -> None:
    class _Unknown:
        ref_type = "item"
        ref_id = "hat"

    assert not await chat_references.validate_selected(
        _Session(_diary()),
        user_id=UID,
        selected_refs=[_Unknown()],
        focus_ref=None,
    )
