"""FCM 푸시·알림 조립 — no-op(자격증명 없음)·설정(기본 on/off)·토큰 로드(mock)."""
import uuid
from types import SimpleNamespace
from datetime import datetime, timezone

from app.services import notify, push

UID_UUID = uuid.uuid4()


class _Scalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return _Scalars(self._items)


class FakeSession:
    def __init__(self, exec_results):
        self.exec_results = list(exec_results)

    async def execute(self, stmt):
        return _Result(self.exec_results.pop(0) if self.exec_results else [])

    async def commit(self):  # #16+#24: 푸시 전 트랜잭션 해제 경로
        pass


async def test_push_noop_without_credentials(monkeypatch):
    monkeypatch.setattr(push.settings, "fcm_service_account_file", "")
    assert await push.send(["tok1"], "제목", "본문") == 0


async def test_push_no_tokens_returns_zero():
    assert await push.send([], "제목", "본문") == 0


async def test_notify_morning_sends_when_enabled(monkeypatch):
    captured = {}

    async def _fake_send(tokens, title, body, **kwargs):
        captured["tokens"] = tokens
        captured["title"] = title
        return len(tokens)

    async def _claim(session, profile, col, **kwargs):
        return True  # 멱등 선점은 test_notify.py가 검증 — 여기선 발송 경로만

    from unittest.mock import AsyncMock
    monkeypatch.setattr(push, "prepare_access_token", AsyncMock(return_value="test-access"))
    monkeypatch.setattr(notify.settings, "morning_push_enabled", True)  # 킬스위치 해제
    monkeypatch.setattr(push, "send", _fake_send)
    monkeypatch.setattr(notify, "_claim_send_slot", _claim)
    session = FakeSession([[], [uuid.uuid4()], ["tok1", "tok2"]])
    n = await notify.notify_morning(
        session, SimpleNamespace(id=UID_UUID, timezone="Asia/Seoul"),
        now=datetime(2026, 9, 9, 0, tzinfo=timezone.utc),
    )
    assert n == 2
    assert captured["tokens"] == ["tok1", "tok2"]


async def test_notify_morning_blocked_by_kill_switch(monkeypatch):
    # SOMA-338: morning_push_enabled=False(기본)면 유저 설정·토큰과 무관하게 발송 안 함(저녁만).
    called = {"sent": False}

    async def _fake_send(tokens, title, body, **kwargs):
        called["sent"] = True
        return len(tokens)

    monkeypatch.setattr(notify.settings, "morning_push_enabled", False)
    monkeypatch.setattr(push, "send", _fake_send)
    session = FakeSession([[], ["tok1", "tok2"]])  # 유저 설정 on·토큰 있어도
    n = await notify.notify_morning(session, SimpleNamespace(id=UID_UUID))
    assert n == 0
    assert called["sent"] is False  # 킬스위치 → 조회·발송 자체를 안 탐


async def test_notify_evening_skipped_when_disabled(monkeypatch):
    called = {"sent": False}

    async def _fake_send(tokens, title, body, **kwargs):
        called["sent"] = True
        return 0

    monkeypatch.setattr(push, "send", _fake_send)
    session = FakeSession([[SimpleNamespace(enabled=False)]])  # evening_chat off
    n = await notify.notify_evening(session, SimpleNamespace(id=UID_UUID))
    assert n == 0
    assert called["sent"] is False  # 설정 off → 발송 안 함


async def test_fcm_payload_expiry_and_network_failure_are_per_device(monkeypatch):
    import httpx
    from datetime import timedelta

    captured = []
    now = datetime(2026, 9, 9, 0, tzinfo=timezone.utc)

    class Clock:
        @staticmethod
        def now(tz):
            return now

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            captured.append(kwargs['json']['message'])
            if len(captured) == 1:
                raise httpx.ConnectError('test')
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(push, 'datetime', Clock)
    monkeypatch.setattr(push, '_access_token', lambda: 'fake-token')
    monkeypatch.setattr(push.httpx, 'AsyncClient', lambda **kwargs: Client())
    expires = now + timedelta(minutes=45)
    assert await push.send(['first', 'second'], 'title', 'body',
                           data={'link': 'diary', 'diary_id': 'id'}, expires_at=expires) == 1
    assert len(captured) == 2
    assert captured[1]['data'] == {'link': 'diary', 'diary_id': 'id'}
    assert captured[1]['android'] == {'ttl': '2700s'}
    assert captured[1]['apns']['headers']['apns-expiration'] == str(int(expires.timestamp()))
    captured.clear()
    assert await push.send(['first'], 'title', 'body', expires_at=now) == 0
    assert not captured
    assert await push.send(['first', 'second'], 'evening', 'body') == 1
    assert 'data' not in captured[1] and 'android' not in captured[1] and 'apns' not in captured[1]
