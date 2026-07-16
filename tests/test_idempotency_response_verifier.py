import json

from scripts import verify_idempotency_responses as verifier
from scripts.verify_idempotency_responses import is_compatible_response


VALID = {
    "greeting": None,
    "user_message": {"message_id": "1", "created_at": "2026-07-16T00:00:00+00:00"},
    "reply": {
        "message_id": "2",
        "content": "응",
        "created_at": "2026-07-16T00:00:00+00:00",
    },
    "tokens_used": 10,
    "tokens_remaining": 29_990,
    "review_prompt": False,
}


def test_verifier_classifies_current_and_legacy_payloads():
    assert is_compatible_response(VALID) is True
    assert is_compatible_response(json.dumps(VALID)) is True  # asyncpg JSONB 기본 반환형
    assert is_compatible_response({"reply": {"content": "legacy"}}) is False


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeCursor:
    def __init__(self, rows):
        self._rows = iter(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._rows)
        except StopIteration:
            raise StopAsyncIteration


class FakeConnection:
    def __init__(self):
        self.rows = [
            {"user_id": "u1", "key": "valid-key", "response": VALID},
            {"user_id": "u2", "key": "legacy-key", "response": {"reply": {}}},
        ]
        self.deleted = None
        self.closed = False

    def cursor(self, query):
        return FakeCursor(self.rows)

    def transaction(self):
        return FakeTransaction()

    async def executemany(self, query, values):
        self.deleted = values

    async def close(self):
        self.closed = True


async def test_default_run_is_read_only(monkeypatch):
    connection = FakeConnection()

    async def _connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(verifier, "_connection_string", lambda: "postgresql://test")
    monkeypatch.setattr(verifier.asyncpg, "connect", _connect)

    assert await verifier.run(delete_invalid=False) == 1
    assert connection.deleted is None
    assert connection.closed is True


async def test_delete_mode_removes_only_invalid_rows(monkeypatch):
    connection = FakeConnection()

    async def _connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(verifier, "_connection_string", lambda: "postgresql://test")
    monkeypatch.setattr(verifier.asyncpg, "connect", _connect)

    assert await verifier.run(delete_invalid=True) == 0
    assert connection.deleted == [("u2", "legacy-key")]
    assert connection.closed is True
