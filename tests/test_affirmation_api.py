from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.core.db import get_session
from app.core.security import get_current_user
from app.main import app
from app.services import affirmation

USER = "00000000-0000-0000-0000-000000000001"
SEOUL = "Asia/Seoul"
ROW = SimpleNamespace(
    local_date=date(2026, 10, 20),
    text_ko="작은 시작도 충분히 의미 있어.",
    text_en="Even a small start is enough.",
    text_ja="小さな一歩にも、ちゃんと意味がある。",
)


@pytest.fixture
def stored():
    return {"row": ROW, "asked": []}


@pytest.fixture
def client(monkeypatch, stored):
    async def session():
        yield SimpleNamespace()

    async def profile(_session, _user_id):
        return SimpleNamespace(timezone=SEOUL)

    async def latest(_session, local_date):
        stored["asked"].append(local_date)
        return stored["row"]

    monkeypatch.setattr(affirmation, "_load_profile", profile)
    monkeypatch.setattr(affirmation, "_latest", latest)
    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_current_user] = lambda: USER
    yield TestClient(app)
    app.dependency_overrides.clear()


def today(zone: str = SEOUL) -> date:
    return affirmation.AppDay.at(datetime.now(timezone.utc), zone).local_date


@pytest.mark.parametrize("header,locale", [("ko-KR", "ko"), ("en", "en"), ("ja-JP", "ja"),
                                           (None, "en"), ("zh-Hant-TW", "en")])
def test_status_returns_the_latest_stored_sentence_for_each_locale(client, stored, header, locale):
    headers = {"X-App-Timezone": SEOUL} | ({"X-App-Locale": header} if header else {})
    response = client.get("/daily-affirmation", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "local_date": today().isoformat(),
        "locale": locale,
        "text": getattr(ROW, f"text_{locale}"),
    }
    assert stored["asked"] == [today()]
    assert response.headers["cache-control"] == "private, no-store"


def test_status_uses_profile_timezone_when_header_is_absent(client, stored):
    response = client.get("/daily-affirmation")
    assert response.status_code == 200
    assert response.json()["local_date"] == today().isoformat()
    assert stored["asked"] == [today()]


def test_status_is_404_when_no_sentence_is_stored(client, stored):
    stored["row"] = None
    response = client.get("/daily-affirmation", headers={"X-App-Timezone": SEOUL})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DAILY_AFFIRMATION_NOT_FOUND"


def test_invalid_timezone_is_rejected_with_no_store(client):
    response = client.get("/daily-affirmation", headers={"X-App-Timezone": "Mars/Olympus"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "APP_TIMEZONE_INVALID"
    assert response.headers["cache-control"] == "private, no-store"


def test_acknowledge_endpoint_is_gone(client):
    response = client.post("/daily-affirmation/acknowledge",
                           json={"local_date": today().isoformat()})
    assert response.status_code in {404, 405}


async def test_latest_reads_the_newest_sentence_on_or_before_the_day():
    statements = []

    class Session:
        async def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    assert await affirmation._latest(Session(), date(2026, 10, 21)) is None
    sql = str(statements[0].compile(dialect=postgresql.dialect(),
                                    compile_kwargs={"literal_binds": True}))
    assert "daily_affirmations.local_date <= '2026-10-21'" in sql
    assert "ORDER BY daily_affirmations.local_date DESC" in sql
    assert "LIMIT 1" in sql
