"""루틴 streak 계산(SOMA-312) + 저녁알림 토큰소진 제외(SOMA-291)."""
from datetime import date, timedelta

from app.services import notify
from app.services.routine import _streak

# ---------------------------------------------------------------------------
# SOMA-312 — streak: 오늘 미완료여도 어제까지 연속 유지, 취소가 streak을 0으로 안 떨굼
# ---------------------------------------------------------------------------
_T = date(2026, 7, 20)


def _d(n: int) -> date:
    return _T - timedelta(days=n)


def test_streak_today_done_counts_today_and_back():
    assert _streak(_T, {_d(0), _d(1), _d(2)}) == 3


def test_streak_today_not_done_keeps_yesterday_run():
    # 어제~10일전 10일 연속, 오늘 아직 미완료 → 10 유지 (구버그: 0)
    assert _streak(_T, {_d(i) for i in range(1, 11)}) == 10


def test_streak_cancel_returns_to_prev():
    done = {_d(i) for i in range(0, 11)}  # 오늘 포함 11일 연속
    assert _streak(_T, done) == 11
    assert _streak(_T, done - {_d(0)}) == 10  # 오늘 완료 취소 → 10 (0 아님) = 이 티켓 핵심


def test_streak_broken_when_yesterday_missing():
    # 그저께부터는 있으나 어제가 비어 있고 오늘도 미완료 → 진짜 끊김 → 0
    assert _streak(_T, {_d(i) for i in range(2, 12)}) == 0


def test_streak_today_only():
    assert _streak(_T, {_T}) == 1


def test_streak_empty():
    assert _streak(_T, set()) == 0


def test_streak_gap_stops():
    # 오늘·어제 연속, 그저께 없음 → 2
    assert _streak(_T, {_d(0), _d(1), _d(3)}) == 2


# ---------------------------------------------------------------------------
# SOMA-291 — 하루 대화량 소진 유저는 저녁 안부 제외
# ---------------------------------------------------------------------------
class _G:
    def __init__(self, remaining):
        self.entitlement = {"tokens_remaining": remaining}


class _Profile:
    id = "00000000-0000-0000-0000-000000000000"


def _patch(monkeypatch, remaining):
    """_enabled=True, gating.resolve=지정 잔량, push.send=스파이. 발송된 body 리스트 반환."""
    sent: list[str] = []

    async def _enabled(*a, **k):
        return True

    async def _tokens(*a, **k):
        return ["tok"]

    async def _send(tokens, title, body):
        sent.append(body)
        return len(tokens)

    async def _resolve(session, uid, now=None):
        return _G(remaining)

    async def _claim(session, profile, col):
        return True  # 멱등 선점은 test_notify.py가 검증 — 여기선 발송 결정 로직만

    from app.services import gating

    monkeypatch.setattr(notify, "_enabled", _enabled)
    monkeypatch.setattr(notify, "_tokens", _tokens)
    monkeypatch.setattr(notify.push, "send", _send)
    monkeypatch.setattr(notify, "_claim_send_slot", _claim)
    monkeypatch.setattr(gating, "resolve", _resolve)
    return sent


async def test_evening_skipped_when_exhausted(monkeypatch):
    sent = _patch(monkeypatch, remaining=0)
    n = await notify.notify_evening(None, _Profile())
    assert n == 0 and sent == []


async def test_evening_sent_when_tokens_remain(monkeypatch):
    sent = _patch(monkeypatch, remaining=5000)
    n = await notify.notify_evening(None, _Profile())
    assert n == 1 and sent == [notify._EVENING["ko"][1]]


async def test_evening_sent_when_unlimited(monkeypatch):
    # tokens_remaining=None = 무제한 tier → 발송
    sent = _patch(monkeypatch, remaining=None)
    n = await notify.notify_evening(None, _Profile())
    assert n == 1 and len(sent) == 1


async def test_evening_skipped_when_disabled(monkeypatch):
    sent = _patch(monkeypatch, remaining=5000)

    async def _disabled(*a, **k):
        return False

    monkeypatch.setattr(notify, "_enabled", _disabled)
    n = await notify.notify_evening(None, _Profile())
    assert n == 0 and sent == []
