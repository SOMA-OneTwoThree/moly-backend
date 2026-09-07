"""Development diary controls must never erase a weekly receipt."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.api import dev
from app.core.errors import AppError
from app.core.time_utils import activity_date_for
from app.schemas.dev import DiaryGenerateRequest, DiaryGenerateResponse
from app.services.diary_generation import DiaryPolicy


@pytest.fixture
def context(monkeypatch):
    profile = SimpleNamespace(id=uuid.uuid4(), timezone="Asia/Seoul", language="ko", nickname=None)
    session = SimpleNamespace(
        get=AsyncMock(return_value=profile), execute=AsyncMock(),
        scalar=AsyncMock(return_value=None), commit=AsyncMock(), rollback=AsyncMock(),
    )
    today = activity_date_for(datetime.now(timezone.utc), profile.timezone)
    start = today - timedelta(days=today.weekday() + 7)
    monkeypatch.setattr(dev.diary_generation, "load_policy", AsyncMock(return_value=DiaryPolicy(start)))
    monkeypatch.setattr(dev, "advisory_xact_lock", AsyncMock())
    monkeypatch.setattr(dev.privacy, "ensure_subject_active", AsyncMock())
    return profile, session, today, start


async def test_weekly_force_rejected_before_delete(context):
    profile, session, today, _ = context
    with pytest.raises(AppError) as exc:
        await dev.generate_diary(
            DiaryGenerateRequest(target_date=today - timedelta(days=1)), str(profile.id), session,
        )
    assert exc.value.code == "DIARY_FORCE_NOT_ALLOWED"
    session.execute.assert_not_awaited()


async def test_weekly_open_day_rejected(context):
    profile, session, today, _ = context
    with pytest.raises(AppError) as exc:
        await dev.generate_diary(
            DiaryGenerateRequest(target_date=today, force=False), str(profile.id), session,
        )
    assert exc.value.http_status == 422
    session.execute.assert_not_awaited()


async def test_missing_policy_is_not_silently_legacy(context, monkeypatch):
    profile, session, _, _ = context
    monkeypatch.setattr(dev.diary_generation, "load_policy", AsyncMock(side_effect=ValueError()))
    with pytest.raises(AppError) as exc:
        await dev.generate_diary(DiaryGenerateRequest(), str(profile.id), session)
    assert exc.value.code == "DIARY_POLICY_UNAVAILABLE"
    session.execute.assert_not_awaited()
    session.rollback.assert_awaited_once()


async def test_legacy_force_cannot_erase_weekly_receipt(context, monkeypatch):
    profile, session, _, _ = context
    monkeypatch.setattr(dev.diary_generation, "load_policy", AsyncMock(return_value=DiaryPolicy()))
    session.scalar.return_value = 1
    async def receipt(_stmt, _params):
        dev.advisory_xact_lock.assert_awaited_once_with(session, profile.id)
        return 1
    session.scalar.side_effect = receipt
    with pytest.raises(AppError) as exc:
        await dev.generate_diary(DiaryGenerateRequest(), str(profile.id), session)
    assert exc.value.code == "DIARY_FORCE_NOT_ALLOWED"
    session.execute.assert_not_awaited()


async def test_weekly_no_entry_response_shape_and_hint(context, monkeypatch):
    profile, session, today, _ = context
    result = dict(created=False, skipped=False, source="none", reason="weekly_exhausted",
                  user_chars=0, gate=60, gate_passed=False, personal_attempted=False)
    monkeypatch.setattr(dev, "effective_token_config", AsyncMock(return_value={"diary_min_user_chars": 60}))
    generate = AsyncMock(return_value=result)
    monkeypatch.setattr(dev.diary_generation, "generate_for_user", generate)
    session.execute.return_value = SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None))
    response = await dev.generate_diary(
        DiaryGenerateRequest(target_date=today - timedelta(days=1), force=False),
        str(profile.id), session,
    )
    parsed = DiaryGenerateResponse.model_validate(response)
    assert parsed.diary is None
    assert parsed.diagnostics.reason == "weekly_exhausted"
    assert "policy" in generate.call_args.kwargs
    assert "모두 지급" in parsed.diagnostics.hint


def test_processed_hint_does_not_recommend_force():
    assert "force" not in dev._hint({"skipped": True})


def test_new_diagnostics_reasons_validate():
    for reason in ("weekly_unavailable", "weekly_exhausted", "no_scheduled_entry"):
        parsed = DiaryGenerateResponse.model_validate({
            "target_date": date(2026, 9, 7), "diary": None,
            "diagnostics": {
                "created": False, "skipped": False, "source": "none", "reason": reason,
                "user_chars": 0, "gate": 60, "gate_passed": False,
                "personal_attempted": False, "hint": "미발행",
            },
        })
        assert parsed.diagnostics.reason == reason
