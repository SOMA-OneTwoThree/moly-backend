from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import routine as routine_api
from app.core.app_day import AppDay, validate_app_timezone
from app.core.db import get_session
from app.core.errors import AppError
from app.core.security import get_current_user
from app.main import app
from app.services.banner_catalog import BannerCatalog


@pytest.mark.parametrize(
    "now,zone,expected,hours",
    [
        ("2026-03-08T05:00:00+00:00", "America/New_York", "2026-03-08", 23),
        ("2026-11-01T04:00:00+00:00", "America/New_York", "2026-11-01", 25),
        ("2026-09-05T15:00:00+00:00", "Asia/Seoul", "2026-09-06", 24),
    ],
)
def test_local_day_uses_calendar_midnight(now, zone, expected, hours):
    day = AppDay.at(datetime.fromisoformat(now), zone)
    assert day.local_date.isoformat() == expected
    assert (day.ends_at - day.served_at).total_seconds() == hours * 3600
    assert day.headers()["X-App-Local-Date"] == expected


@pytest.mark.parametrize("name", ["", "../Asia/Seoul", "Mars/Olympus", "x" * 65])
def test_timezone_invalid(name):
    with pytest.raises(AppError) as exc:
        validate_app_timezone(name)
    assert exc.value.code == "APP_TIMEZONE_INVALID"


@pytest.fixture
def client():
    async def session():
        yield SimpleNamespace()

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_current_user] = lambda: "00000000-0000-0000-0000-000000000001"
    old_catalog = getattr(app.state, "banner_catalog", None)
    app.state.banner_catalog = BannerCatalog.load()
    yield TestClient(app)
    app.state.banner_catalog = old_catalog
    app.dependency_overrides.clear()


def query(**changes):
    return dict(
        placement="home_blind",
        schema_version=1,
        platform="ios",
        app_version="1.0.0",
        capabilities=["banner_canvas_v1"],
        **changes,
    )


def test_disabled_feed_does_not_need_database(client):
    response = client.get("/banners", params=query())
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize(
    "params,header,code",
    [
        ({"placement": "wrong"}, {}, "BANNER_PLACEMENT_UNSUPPORTED"),
        ({"schema_version": 2}, {}, "BANNER_SCHEMA_UNSUPPORTED"),
        ({}, {"X-App-Timezone": "wrong"}, "APP_TIMEZONE_INVALID"),
        ({"capabilities": ["wrong-value"]}, {}, "VALIDATION"),
    ],
)
def test_invalid_requests_have_no_store(client, params, header, code):
    response = client.get("/banners", params=query() | params, headers=header)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "private, no-store"


def test_unavailable_catalog_is_not_empty_success(client):
    app.state.banner_catalog = None
    response = client.get("/banners", params=query())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "BANNERS_UNAVAILABLE"
    assert client.get("/health").status_code == 200


def test_routine_204_has_same_date_headers(client, monkeypatch):
    seen = []

    async def uncomplete(session, user_id, routine_id, day=None):
        seen.append(day)

    monkeypatch.setattr(routine_api.routine, "uncomplete", uncomplete)
    response = client.delete(
        "/routines/test/complete", headers={"X-App-Timezone": "America/Los_Angeles"}
    )
    assert response.status_code == 204
    assert response.content == b""
    assert date.fromisoformat(response.headers["x-app-local-date"]) == seen[0].local_date
    assert response.headers["x-app-day-ends-at"].endswith("Z")


def test_routine_missing_timezone_uses_profile(client, monkeypatch):
    async def profile(session, user):
        return SimpleNamespace(timezone="Pacific/Honolulu")

    async def listing(session, user_id, day=None):
        assert (
            day.local_date
            == datetime.now(timezone.utc)
            .astimezone(__import__("zoneinfo").ZoneInfo("Pacific/Honolulu"))
            .date()
        )
        return {"data": []}

    monkeypatch.setattr(routine_api, "_load_profile", profile)
    monkeypatch.setattr(routine_api.routine, "list_routines", listing)
    response = client.get("/routines")
    assert response.status_code == 200
    assert "x-app-local-date" in response.headers
