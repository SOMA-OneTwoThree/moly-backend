"""슬랙 Incoming Webhook 헬퍼·워커 요약 — no-op·전송·오류·포맷."""
from datetime import datetime, timezone

import httpx

from app.services import slack_notify
from worker.tick import _build_summary

_URL = "https://hooks.slack.com/services/test"
_COUNTS_OK = {
    "diaries": 42, "diary_llm": 30, "diary_preset": 12, "diary_failed": 0,
    "memory_ok": 30, "memory_failed": 0,
    "morning": 38, "evening": 0,
    "diary_attempted": 42, "users": 50,
}
_COUNTS_FAIL = {**_COUNTS_OK, "diary_failed": 2, "memory_failed": 4}


# ---------------------------------------------------------------------------
# slack_notify.send_summary
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.text = "ok"


class _FakeClient:
    def __init__(self, response=None, raises=None):
        self._response = response or _FakeResponse()
        self._raises = raises
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, url, json=None):
        if self._raises:
            raise self._raises
        self.calls.append({"url": url, "json": json})
        return self._response


async def test_noop_when_url_empty(monkeypatch):
    """SLACK_WEBHOOK_URL 미설정 → HTTP 호출 없이 조용히 반환."""
    monkeypatch.setattr(slack_notify.settings, "slack_webhook_url", "")
    fake = _FakeClient()
    monkeypatch.setattr(slack_notify.httpx, "AsyncClient", lambda **kw: fake)
    await slack_notify.send_summary("테스트")
    assert fake.calls == []


async def test_sends_json_payload(monkeypatch):
    """URL 설정 시 {"text": ...} JSON으로 POST 요청."""
    monkeypatch.setattr(slack_notify.settings, "slack_webhook_url", _URL)
    fake = _FakeClient()
    monkeypatch.setattr(slack_notify.httpx, "AsyncClient", lambda **kw: fake)
    await slack_notify.send_summary("요약 텍스트")
    assert len(fake.calls) == 1
    assert fake.calls[0]["url"] == _URL
    assert fake.calls[0]["json"] == {"text": "요약 텍스트"}


async def test_no_raise_on_connect_error(monkeypatch):
    """네트워크 오류 시 예외를 올리지 않는다(배치 미중단)."""
    monkeypatch.setattr(slack_notify.settings, "slack_webhook_url", _URL)
    fake = _FakeClient(raises=httpx.ConnectError("refused"))
    monkeypatch.setattr(slack_notify.httpx, "AsyncClient", lambda **kw: fake)
    await slack_notify.send_summary("요약")  # must not raise


async def test_no_raise_on_non_200(monkeypatch):
    """HTTP 500 응답도 예외를 올리지 않는다."""
    monkeypatch.setattr(slack_notify.settings, "slack_webhook_url", _URL)
    fake = _FakeClient(response=_FakeResponse(500))
    monkeypatch.setattr(slack_notify.httpx, "AsyncClient", lambda **kw: fake)
    await slack_notify.send_summary("요약")  # must not raise


# ---------------------------------------------------------------------------
# _build_summary 포맷
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc)


def test_build_summary_no_failures():
    """실패 없으면 ⚠️ 없이 깔끔한 요약. 시각은 KST 우선 + UTC 병기."""
    msg = _build_summary(_NOW, _COUNTS_OK, elapsed=45.3)
    assert msg.startswith("[워커 요약]")
    assert "⚠️" not in msg
    assert "2026-07-20 13:00 KST" in msg   # 04:00 UTC = 13:00 KST
    assert "(04:00 UTC)" in msg
    assert "개인 30" in msg
    assert "프리셋 12" in msg
    assert "성공 30" in msg
    assert "아침 38건" in msg
    assert "전체 유저 50명" in msg
    assert "45.3s" in msg


def test_build_summary_shows_target_timezone():
    """active_tzs 주면 '대상 타임존' 라인에 나라·현지시간·오프셋 표기."""
    msg = _build_summary(_NOW, _COUNTS_OK, elapsed=1.0, active_tzs={"Europe/Prague"})
    assert "대상 타임존:" in msg
    assert "체코(Europe/Prague)" in msg
    assert "현지 06:00" in msg   # 04:00 UTC = 06:00 프라하(CEST, UTC+2)
    assert "UTC+2" in msg


def test_build_summary_no_timezone_line_when_empty():
    """active_tzs 없으면 대상 타임존 라인 생략(하위호환)."""
    msg = _build_summary(_NOW, _COUNTS_OK, elapsed=1.0)
    assert "대상 타임존" not in msg


def test_build_summary_with_failures():
    """실패 있으면 ⚠️ 프리픽스 + 실패 수치 강조."""
    msg = _build_summary(_NOW, _COUNTS_FAIL, elapsed=10.0)
    assert msg.startswith("⚠️ [워커 요약]")
    assert "실패 ⚠️ 2건" in msg   # diary_failed
    assert "⚠️ 4" in msg          # memory_failed
