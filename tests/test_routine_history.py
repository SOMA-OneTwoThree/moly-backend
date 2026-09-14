from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.api import routine as routine_api
from app.core.app_day import AppDay
from app.core.db import get_session
from app.core.errors import AppError
from app.core.security import get_current_user
from app.main import app
from app.services import routine

UID = UUID('00000000-0000-0000-0000-000000000001')
DAY = AppDay.at(datetime(2026, 9, 13, 15, tzinfo=timezone.utc), 'Asia/Seoul')


@pytest.fixture
def client(monkeypatch):
    async def session():
        yield SimpleNamespace()

    async def profile(session, user_id):
        return SimpleNamespace(id=UID, timezone='Asia/Seoul', language='ko')

    async def request_day(response: Response):
        response.headers.update(DAY.headers())
        return DAY

    monkeypatch.setattr(routine, '_load_profile', profile)
    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_current_user] = lambda: str(UID)
    app.dependency_overrides[routine_api.request_day] = request_day
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize('value', ['', '2026-02-30', '2026-9-1', 'yesterday', '2026-09-01T00:00:00Z', '1788220800'])
def test_invalid_date(client, value):
    response = client.get('/routines/history', params={'date': value})
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'VALIDATION'


def test_missing_and_future_date(client):
    assert client.get('/routines/history').status_code == 422
    response = client.get('/routines/history?date=2026-09-15')
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'VALIDATION'


def test_selected_date_is_separate_from_actual_today_headers(client, monkeypatch):
    async def history(session, user_id, selected_date, day, timezone_name):
        assert user_id == str(UID)
        assert selected_date == date(2025, 12, 31)
        assert day.local_date == date(2026, 9, 14)
        assert timezone_name == 'Asia/Seoul'
        return {'date': selected_date, 'data': []}

    monkeypatch.setattr(routine, 'history', history)
    response = client.get('/routines/history?date=2025-12-31', headers={'X-App-Timezone': 'Asia/Seoul'})
    assert response.status_code == 200, response.text
    assert response.json() == {'date': '2025-12-31', 'data': []}
    assert response.headers['x-app-local-date'] == '2026-09-14'
    assert response.headers['cache-control'] == 'private, no-store'


def test_missing_profile_is_not_empty_success(client, monkeypatch):
    async def missing(session, user_id):
        raise AppError('NOT_FOUND', 404, 'profile missing')

    monkeypatch.setattr(routine, '_load_profile', missing)
    assert client.get('/routines/history?date=2026-09-01').status_code == 404


def test_unauthenticated_history_is_rejected(client):
    del app.dependency_overrides[get_current_user]
    assert client.get('/routines/history?date=2026-09-01').status_code == 401
