"""운세 one-shot 채팅 컨텍스트의 안전·TOCTOU·파생 격리 계약."""
from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from pydantic import ValidationError

from app.core import errors
from app.models.fortune import DailyFortune, FortuneProfile
from app.schemas.chat import PostMessageRequest
from app.services import chat, chat_turns, fortune_chat, fortune_ephemeris


TODAY = date(2026, 8, 27)
UID = uuid.UUID("10000000-0000-4000-8000-000000000001")


def _profile():
    return SimpleNamespace(revision=7)


def _daily():
    return SimpleNamespace(
        fortune_date=TODAY,
        timezone_snapshot="Asia/Seoul",
        profile_revision=7,
        result_schema_version=3,
        semantic_result={
            "schema_version": 3,
            "overall": {"score": 78},
            "categories": {
                "love": {"score": 72}, "money": {"score": 61},
                "work": {"score": 88}, "energy": {"score": 57},
            },
        },
        copy_by_locale={
            "ko": {
                "overall": {
                    "headline": "미뤄 둔 일을 시작하기 좋은 날이야",
                    "flow": ["마음이 가벼워.", "일의 순서가 보여.", "여유도 충분해."],
                    "do": "가장 중요한 일 하나부터 끝내기",
                    "pause": "상대의 답을 재촉하기",
                },
                "categories": {
                    key: {"text": ["첫 번째 설명.", "두 번째 설명."]}
                    for key in ("love", "money", "work", "energy")
                },
                "lucky_color": {"key": "purple", "name": "보라"},
            },
        },
        unlock_state="unlocked",
        revealed_at=datetime(2026, 8, 27, 1, tzinfo=timezone.utc),
        ephemeris_version=fortune_ephemeris.EPHEMERIS_VERSION,
        rule_version="fortune-rules.v2.1",
        copy_version="fortune-copy.v2-seed.3",
    )


class _Session:
    def __init__(self, profile=None, daily=None):
        self.profile = profile or _profile()
        self.daily = daily or _daily()

    async def get(self, model, _key, **_kwargs):
        if model is FortuneProfile:
            return self.profile
        if model is DailyFortune:
            return self.daily
        raise AssertionError(f"unexpected model: {model}")


def _enable(monkeypatch):
    monkeypatch.setattr(fortune_chat.settings, "fortune_enabled", True)
    monkeypatch.setattr(fortune_chat.settings, "fortune_chat_enabled", True)


def test_context_ref_is_strict_and_requires_locale():
    with pytest.raises(ValidationError):
        PostMessageRequest.model_validate(
            {"text": "풀어줘", "context_ref": {"type": "daily_fortune", "local_date": str(TODAY)}}
        )
    with pytest.raises(ValidationError):
        PostMessageRequest.model_validate(
            {
                "text": "풀어줘",
                "context_ref": {
                    "type": "daily_fortune",
                    "local_date": str(TODAY),
                    "locale": "ko",
                    "score": 78,
                },
            }
        )


def test_request_hash_includes_full_context_ref():
    base = dict(text_value="풀어줘", greeting_id=None, diary_references=False)
    no_ref = chat_turns.request_hash(**base)
    ko = chat_turns.request_hash(
        **base, context_ref={"type": "daily_fortune", "local_date": str(TODAY), "locale": "ko"}
    )
    next_day = chat_turns.request_hash(
        **base,
        context_ref={
            "type": "daily_fortune",
            "local_date": str(TODAY + timedelta(days=1)),
            "locale": "ko",
        },
    )
    assert len({no_ref, ko, next_day}) == 3


@pytest.mark.asyncio
async def test_snapshot_renders_only_public_result(monkeypatch):
    _enable(monkeypatch)
    snapshot = await fortune_chat.load_snapshot(
        _Session(), user_id=UID, local_date=TODAY, locale="ko", account_timezone="Asia/Seoul"
    )
    assert "미뤄 둔 일을 시작" in snapshot.block
    assert "행운 지수: 78/100" in snapshot.block
    assert "애정 72, 금전 61, 일 88, 활력 57" in snapshot.block
    assert "birth" not in snapshot.block.lower()
    assert "advert" not in snapshot.block.lower()


@pytest.mark.asyncio
async def test_revalidate_rejects_changed_result(monkeypatch):
    _enable(monkeypatch)
    session = _Session()
    snapshot = await fortune_chat.load_snapshot(
        session, user_id=UID, local_date=TODAY, locale="ko", account_timezone="Asia/Seoul"
    )
    session.daily.semantic_result = {
        **session.daily.semantic_result,
        "overall": {**session.daily.semantic_result["overall"], "score": 77},
    }
    with pytest.raises(errors.AppError) as caught:
        await fortune_chat.revalidate(
            session,
            user_id=UID,
            snapshot=snapshot,
            current_local_date=TODAY,
            account_timezone="Asia/Seoul",
        )
    assert caught.value.code == "FORTUNE_CONTEXT_STALE"


@pytest.mark.asyncio
async def test_revalidate_fails_closed_when_chat_flag_turns_off(monkeypatch):
    """Phase 1 뒤 kill switch가 내려가면 진행 중 답변도 저장하지 않는다."""
    _enable(monkeypatch)
    session = _Session()
    snapshot = await fortune_chat.load_snapshot(
        session, user_id=UID, local_date=TODAY, locale="ko", account_timezone="Asia/Seoul"
    )
    monkeypatch.setattr(fortune_chat.settings, "fortune_chat_enabled", False)
    with pytest.raises(errors.AppError):
        await fortune_chat.revalidate(
            session,
            user_id=UID,
            snapshot=snapshot,
            current_local_date=TODAY,
            account_timezone="Asia/Seoul",
        )


def test_crisis_check_precedes_fortune_fetch_in_chat_source():
    source = inspect.getsource(chat.post_message)
    crisis = source.index("crisis_now = context_safety.is_continuing_distress")
    fetch = source.index("fortune_snapshot = await fortune_chat.load_snapshot")
    assert crisis < fetch
    assert "and not crisis_now" in source[crisis:fetch]


def test_fortune_message_kinds_are_allowed_by_database_schema():
    """ORM 값만 늘리고 PostgreSQL CHECK를 그대로 두면 첫 root insert가 전부 실패한다."""
    schema = Path("db/schema.sql").read_text(encoding="utf-8")
    assert "fortune_context_root" in schema
    assert "fortune_derived" in schema


def test_long_lived_derivations_filter_fortune_message_kinds():
    from app.services import checkpoint_repo, diary_generation
    from worker import contract_jobs, mem0_jobs

    checkpoint_source = inspect.getsource(chat._enqueue_checkpoint)
    checkpoint_reload = str(checkpoint_repo._RANGE_SQL)
    diary_source = inspect.getsource(diary_generation._day_messages)
    assert "fortune_context_root" in checkpoint_source and "fortune_derived" in checkpoint_source
    assert "fortune_context_root" in checkpoint_reload and "fortune_derived" in checkpoint_reload
    assert 'Message.kind == "normal"' in diary_source
    assert "kind = 'normal'" in str(mem0_jobs._SOURCE_MESSAGES)
    assert "kind = 'normal'" in str(contract_jobs._MESSAGES)
