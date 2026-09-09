from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from app.services import notify, push

NOW = datetime(2026, 9, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def delivery(monkeypatch):
    monkeypatch.setattr(notify.settings, 'morning_push_enabled', True)
    calls = SimpleNamespace(
        enabled=AsyncMock(return_value=True), diary=AsyncMock(return_value=uuid.uuid4()),
        tokens=AsyncMock(return_value=['token', 'token', '', 'second']),
        claim=AsyncMock(return_value=True), send=AsyncMock(return_value=2),
        profile=SimpleNamespace(id=uuid.uuid4(), timezone='Asia/Seoul', language='ko'),
    )
    for name in ('enabled', 'tokens'):
        monkeypatch.setattr(notify, '_' + name, getattr(calls, name))
    monkeypatch.setattr(notify, '_morning_diary_id', calls.diary)
    monkeypatch.setattr(notify, '_claim_send_slot', calls.claim)
    monkeypatch.setattr(push, 'send', calls.send)
    calls.auth = AsyncMock(return_value="test-access")
    monkeypatch.setattr(push, "prepare_access_token", calls.auth)
    return calls


async def test_fresh_diary_sends_compatible_link_and_deduplicates_tokens(delivery):
    assert await notify.notify_morning(None, delivery.profile, now=NOW) == 2
    delivery.send.assert_awaited_once_with(
        ['token', 'second'], '캐피', '캐피의 새 일기가 도착했어',
        data={'link': 'diary', 'diary_id': str(delivery.diary.return_value)},
        expires_at=NOW.replace(hour=1), access_token="test-access",
    )
    delivery.claim.assert_awaited_once_with(None, delivery.profile, 'morning_notified_at', now=NOW)


@pytest.mark.parametrize('gate', ['disabled', 'no_diary', 'no_token', 'claimed'])
async def test_ineligible_does_not_send(delivery, gate):
    if gate == 'disabled':
        delivery.enabled.return_value = False
    if gate == 'no_diary':
        delivery.diary.return_value = None
    if gate == 'no_token':
        delivery.tokens.return_value = []
    if gate == 'claimed':
        delivery.claim.return_value = False
    assert await notify.notify_morning(None, delivery.profile, now=NOW) == 0
    delivery.send.assert_not_awaited()
    if gate != 'claimed':
        delivery.claim.assert_not_awaited()


@pytest.mark.parametrize('hour', [8, 10, 20, 23])
async def test_outside_local_morning_does_not_even_query(delivery, hour):
    now = datetime(2026, 9, 9, hour - 9 if hour >= 9 else 23, tzinfo=timezone.utc)
    assert await notify.notify_morning(None, delivery.profile, now=now) == 0
    delivery.enabled.assert_not_awaited()
    delivery.claim.assert_not_awaited()


async def test_failed_delivery_does_not_release_or_repeat_claim(delivery):
    delivery.send.return_value = 0
    assert await notify.notify_morning(None, delivery.profile, now=NOW) == 0
    delivery.claim.return_value = False
    assert await notify.notify_morning(None, delivery.profile, now=NOW) == 0
    assert delivery.send.await_count == 1


@pytest.mark.parametrize("raises", [False, True])
async def test_auth_preparation_failure_can_retry_without_consuming_slot(delivery, raises):
    if raises:
        delivery.auth.side_effect = RuntimeError("test auth failure")
    else:
        delivery.auth.return_value = None
    assert await notify.notify_morning(None, delivery.profile, now=NOW) == 0
    delivery.claim.assert_not_awaited()
    delivery.send.assert_not_awaited()
    delivery.auth.side_effect = None
    delivery.auth.return_value = "test-access"
    assert await notify.notify_morning(None, delivery.profile, now=NOW) == 2
