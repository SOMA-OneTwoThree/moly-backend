"""동일 채팅 멱등키가 사용자 잠금을 기다린 뒤 중복 실행되지 않는지 검증."""
from types import SimpleNamespace

from app.models.idempotency_key import IdempotencyKey
from app.services import chat, gating, llm
from tests.test_chat import FakeSession, UID


class _ConcurrentRetrySession(FakeSession):
    def __init__(self, cached):
        super().__init__()
        self.cached = cached
        self.idempotency_lookups = 0

    async def get(self, model, key):
        if model is IdempotencyKey:
            self.idempotency_lookups += 1
            return None if self.idempotency_lookups == 1 else self.cached
        return await super().get(model, key)


async def test_waiting_duplicate_rechecks_idempotency_after_user_lock(monkeypatch):
    async def _must_not_run(*args, **kwargs):
        raise AssertionError("잠금 대기 중 완료된 요청을 다시 실행하면 안 됨")

    monkeypatch.setattr(gating, "resolve", _must_not_run)
    monkeypatch.setattr(llm, "generate", _must_not_run)
    cached_response = {
        "greeting": None,
        "user_message": {
            "message_id": "1",
            "created_at": "2026-07-20T00:00:00+00:00",
        },
        "reply": {
            "message_id": "2",
            "content": "먼저 끝난 응답",
            "created_at": "2026-07-20T00:00:00+00:00",
        },
        "tokens_used": 100,
        "tokens_remaining": 19_900,
        "review_prompt": False,
    }
    session = _ConcurrentRetrySession(SimpleNamespace(response=cached_response))

    out = await chat.post_message(
        session,
        UID,
        SimpleNamespace(text="동시에 보낸 메시지", greeting_id=None),
        "same-key",
    )

    assert out.model_dump(mode="json") == cached_response
    assert session.idempotency_lookups == 2
    assert session.added == []
    assert session.committed is False
