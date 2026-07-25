"""알림 발송 멱등 게이트(SOMA-348) — claim 실패 시 push 미발송(DB·push mock)."""
import uuid
from types import SimpleNamespace

from app.services import gating, notify, push

UID = uuid.uuid4()


def _profile(**over):
    p = {"id": UID, "timezone": "Asia/Seoul", "language": "ko"}
    p.update(over)
    return SimpleNamespace(**p)


async def test_evening_skips_when_already_notified(monkeypatch):
    """오늘 이미 발송(claim False)이면 저녁 푸시를 다시 보내지 않는다."""
    sent = {}

    async def _enabled(session, uid, t):
        return True

    async def _claim(session, profile, col):
        return False  # 이미 발송됨

    async def _resolve(session, uid):
        return SimpleNamespace(entitlement={"tokens_remaining": None})

    async def _send(tokens, title, body):
        sent["called"] = True
        return 1

    monkeypatch.setattr(notify, "_enabled", _enabled)
    monkeypatch.setattr(notify, "_claim_send_slot", _claim)
    monkeypatch.setattr(gating, "resolve", _resolve)
    monkeypatch.setattr(push, "send", _send)
    assert await notify.notify_evening(None, _profile()) == 0
    assert "called" not in sent  # push 미호출


async def test_evening_sends_and_claims_evening_column(monkeypatch):
    """최초 발송(claim True)이면 저녁 푸시를 보내고 evening 컬럼을 선점한다."""
    seen = {}

    async def _enabled(session, uid, t):
        return True

    async def _claim(session, profile, col):
        seen["col"] = col
        return True

    async def _resolve(session, uid):
        return SimpleNamespace(entitlement={"tokens_remaining": None})

    async def _tokens(session, uid):
        return ["tok"]

    async def _send(tokens, title, body):
        seen["title"] = title
        return 1

    monkeypatch.setattr(notify, "_enabled", _enabled)
    monkeypatch.setattr(notify, "_claim_send_slot", _claim)
    monkeypatch.setattr(notify, "_tokens", _tokens)
    monkeypatch.setattr(gating, "resolve", _resolve)
    monkeypatch.setattr(push, "send", _send)
    assert await notify.notify_evening(None, _profile()) == 1
    assert seen["col"] == "evening_notified_at" and seen["title"] == "캐피"


async def test_evening_exhausted_does_not_claim(monkeypatch):
    """토큰 소진 유저는 발송도 선점도 하지 않는다(claim 전에 스킵)."""
    claimed = {}

    async def _enabled(session, uid, t):
        return True

    async def _claim(session, profile, col):
        claimed["called"] = True
        return True

    async def _resolve(session, uid):
        return SimpleNamespace(entitlement={"tokens_remaining": 0})  # 소진

    monkeypatch.setattr(notify, "_enabled", _enabled)
    monkeypatch.setattr(notify, "_claim_send_slot", _claim)
    monkeypatch.setattr(gating, "resolve", _resolve)
    assert await notify.notify_evening(None, _profile()) == 0
    assert "called" not in claimed  # 선점 안 함 → 다른 조건 회복 시 재평가 가능


async def test_morning_skips_when_already_notified(monkeypatch):
    """아침 푸시도 멱등 — 킬스위치 on 상태에서 claim False면 미발송."""
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "morning_push_enabled", True)
    sent = {}

    async def _enabled(session, uid, t):
        return True

    async def _claim(session, profile, col):
        return False

    async def _send(tokens, title, body):
        sent["called"] = True
        return 1

    monkeypatch.setattr(notify, "_enabled", _enabled)
    monkeypatch.setattr(notify, "_claim_send_slot", _claim)
    monkeypatch.setattr(push, "send", _send)
    assert await notify.notify_morning(None, _profile()) == 0
    assert "called" not in sent
