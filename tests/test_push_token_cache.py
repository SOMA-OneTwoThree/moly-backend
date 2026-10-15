"""FCM 액세스 토큰 캐시 — 만료 전까지 재사용하고, 실패·미설정은 종전처럼 다음 호출에서 다시 시도한다."""
import threading
import time
from datetime import timedelta

import pytest
from google.auth import _helpers
from google.oauth2 import credentials as oauth2_credentials

from app.services import push


class _Creds(oauth2_credentials.Credentials):
    """실제 google-auth 자격증명의 token_state(만료·임박 판정)를 쓰고 refresh만 가짜로 바꾼다."""

    def __init__(self, *, lifetime=timedelta(hours=1), delay=0.0, fail_first=False):
        super().__init__(token=None)
        self.refreshes = 0
        self._lifetime = lifetime
        self._delay = delay
        self._fail_first = fail_first

    def refresh(self, request):
        self.refreshes += 1
        if self._delay:
            time.sleep(self._delay)
        if self._fail_first and self.refreshes == 1:
            raise RuntimeError("token endpoint down")
        self.token = f"tok{self.refreshes}"
        self.expiry = _helpers.utcnow() + self._lifetime


@pytest.fixture(autouse=True)
def _project(monkeypatch):
    monkeypatch.setattr(push.settings, "fcm_project_id", "proj")
    monkeypatch.setattr(push, "_refresh_request", lambda: None)
    monkeypatch.setattr(push, "_creds", None)


def test_refreshes_once_and_reuses(monkeypatch):
    creds = _Creds()
    monkeypatch.setattr(push, "_load_credentials", lambda: creds)
    assert [push._access_token() for _ in range(5)] == ["tok1"] * 5
    assert creds.refreshes == 1


def test_refreshes_again_when_close_to_expiry(monkeypatch):
    creds = _Creds(lifetime=timedelta(minutes=2))  # 라이브러리 임박 기준(3분 45초) 안 → 매번 갱신
    monkeypatch.setattr(push, "_load_credentials", lambda: creds)
    assert push._access_token() == "tok1"
    assert push._access_token() == "tok2" and creds.refreshes == 2


def test_refresh_failure_propagates_and_next_call_retries(monkeypatch):
    """아침 푸시는 인증 준비 실패면 선점하지 않고 다시 평가한다 — 실패를 삼키거나 캐시하면 안 된다."""
    creds = _Creds(fail_first=True)
    monkeypatch.setattr(push, "_load_credentials", lambda: creds)
    with pytest.raises(RuntimeError):
        push._access_token()
    assert push._access_token() == "tok2" and creds.refreshes == 2


def test_concurrent_threads_refresh_once(monkeypatch):
    creds = _Creds(delay=0.05)
    monkeypatch.setattr(push, "_load_credentials", lambda: creds)
    out = []
    threads = [threading.Thread(target=lambda: out.append(push._access_token())) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert out == ["tok1"] * 5 and creds.refreshes == 1


def test_noop_without_credentials_is_not_cached(monkeypatch):
    loads = []
    monkeypatch.setattr(push, "_load_credentials", lambda: loads.append(1))
    assert push._access_token() is None and push._access_token() is None
    assert len(loads) == 2 and push._creds is None


def test_noop_without_project(monkeypatch):
    monkeypatch.setattr(push.settings, "fcm_project_id", "")
    monkeypatch.setattr(push, "_load_credentials", lambda: pytest.fail("자격증명을 찾지 않아야 한다"))
    assert push._access_token() is None
