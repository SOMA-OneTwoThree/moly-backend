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
def client(monkeypatch):
    async def session():
        yield SimpleNamespace()

    async def profile(_session, _user_id):
        return SimpleNamespace(timezone=SEOUL)

    monkeypatch.setattr(affirmation, "_load_profile", profile)
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
    }
    assert response.headers["cache-control"] == "private, no-store"


def test_status_uses_profile_timezone_when_header_is_absent(client):
    response = client.get("/daily-affirmation")
    assert response.status_code == 200
    assert response.json()["local_date"] == today().isoformat()


def test_invalid_timezone_is_rejected_with_no_store(client):
    response = client.get("/daily-affirmation", headers={"X-App-Timezone": "Mars/Olympus"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "APP_TIMEZONE_INVALID"
    assert response.headers["cache-control"] == "private, no-store"


def test_acknowledge_endpoint_is_gone(client):
    response = client.post("/daily-affirmation/acknowledge",
                           json={"local_date": today().isoformat()})
    assert response.status_code in {404, 405}
