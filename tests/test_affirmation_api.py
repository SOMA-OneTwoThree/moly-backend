from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.security import get_current_user
from app.main import app
from app.services import affirmation

USER = "00000000-0000-0000-0000-000000000001"
SEOUL = "Asia/Seoul"


@pytest.fixture
def stats():
    return SimpleNamespace(affirmation_acknowledged_at=None)


@pytest.fixture
def client(monkeypatch, stats):
    async def session():
        yield SimpleNamespace(commit=_nothing)

    async def _nothing(*args, **kwargs):
        return None

    async def profile(_session, _user_id):
        return SimpleNamespace(timezone=SEOUL)

    async def acknowledged_at(_session, _uid, local_date):
        return stats.affirmation_acknowledged_at

    async def daily(_session, _uid, _activity_date):
        return stats

    monkeypatch.setattr(affirmation, "_load_profile", profile)
    monkeypatch.setattr(affirmation, "_acknowledged_at", acknowledged_at)
    monkeypatch.setattr(affirmation, "_daily", daily)
    monkeypatch.setattr(affirmation, "advisory_xact_lock", _nothing)
    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_current_user] = lambda: USER
    yield TestClient(app)
    app.dependency_overrides.clear()


def today(zone: str = SEOUL) -> date:
    return affirmation.AppDay.at(datetime.now(timezone.utc), zone).local_date


@pytest.mark.parametrize("header,locale", [("ko-KR", "ko"), ("en", "en"), ("ja-JP", "ja"),
                                           (None, "en"), ("zh-Hant-TW", "en")])
def test_status_returns_todays_sentence_for_each_locale(client, header, locale):
    headers = {"X-App-Timezone": SEOUL} | ({"X-App-Locale": header} if header else {})
    response = client.get("/daily-affirmation", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    item = affirmation.daily_affirmation(today())
    assert body == {
        "local_date": today().isoformat(),
        "locale": locale,
        "affirmation": {"id": item.id, "text": item.text.for_locale(locale)},
        "acknowledged": False,
    }
    assert response.headers["cache-control"] == "private, no-store"


def test_status_uses_profile_timezone_when_header_is_absent(client):
    response = client.get("/daily-affirmation")
    assert response.status_code == 200
    assert response.json()["local_date"] == today().isoformat()


def test_status_reports_acknowledged_after_the_marker_exists(client, stats):
    stats.affirmation_acknowledged_at = datetime.now(timezone.utc)
    response = client.get("/daily-affirmation", headers={"X-App-Timezone": SEOUL})
    assert response.status_code == 200
    assert response.json()["acknowledged"] is True


def test_acknowledge_is_idempotent_for_the_same_local_date(client, stats):
    payload = {"local_date": today().isoformat()}
    first = client.post("/daily-affirmation/acknowledge", json=payload,
                        headers={"X-App-Timezone": SEOUL})
    assert first.status_code == 200, first.text
    assert first.json() == {"local_date": payload["local_date"], "acknowledged": True}
    assert first.headers["cache-control"] == "private, no-store"
    marked = stats.affirmation_acknowledged_at
    assert marked is not None
    second = client.post("/daily-affirmation/acknowledge", json=payload,
                         headers={"X-App-Timezone": SEOUL})
    assert second.status_code == 200
    assert second.json() == first.json()
    assert stats.affirmation_acknowledged_at == marked  # 첫 확인 시각을 덮어쓰지 않는다


def test_acknowledge_rejects_a_stale_local_date(client, stats):
    response = client.post(
        "/daily-affirmation/acknowledge",
        json={"local_date": date(2020, 1, 1).isoformat()},
        headers={"X-App-Timezone": SEOUL},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DAILY_AFFIRMATION_STALE"
    assert stats.affirmation_acknowledged_at is None


@pytest.mark.parametrize("method", ["get", "post"])
def test_invalid_timezone_is_rejected_with_no_store(client, method):
    headers = {"X-App-Timezone": "Mars/Olympus"}
    if method == "get":
        response = client.get("/daily-affirmation", headers=headers)
    else:
        response = client.post("/daily-affirmation/acknowledge",
                               json={"local_date": today().isoformat()}, headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "APP_TIMEZONE_INVALID"
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("payload", [{}, {"local_date": "not-a-date"},
                                     {"local_date": "2026-09-09", "extra": 1}])
def test_acknowledge_rejects_malformed_bodies(client, payload):
    response = client.post("/daily-affirmation/acknowledge", json=payload,
                           headers={"X-App-Timezone": SEOUL})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION"
