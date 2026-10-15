"""루틴 개편 API 표면 — 통계 기간 검증·응답 형태, 템플릿 선택 검증, 스킵 경로(DB 없음)."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.api import routine as routine_api
from app.core.app_day import AppDay
from app.core.db import get_session
from app.core.security import get_current_user
from app.main import app
from app.services import routine
from app.services.routine_records import RoutineRecord

UID = UUID('00000000-0000-0000-0000-000000000001')
DAY = AppDay.at(datetime(2026, 10, 8, 3, tzinfo=timezone.utc), 'Asia/Seoul')  # 목요일
TODAY = DAY.local_date
ALL = (1, 2, 3, 4, 5, 6, 7)


@pytest.fixture
def client(monkeypatch):
    async def session():
        yield SimpleNamespace()

    async def profile(session, user_id):
        return SimpleNamespace(id=UID, timezone='Asia/Seoul', language='en')

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


def _row(index, **over):
    base = dict(
        id=UUID(int=index), name=f'routine {index}', name_i18n=None, icon='seedling', color='peach',
        deleted_at=None,
    )
    return SimpleNamespace(**base | over)


@pytest.fixture
def records(monkeypatch):
    active = _row(1, name_i18n={'ko': '물 마시기', 'en': 'Drink water'}, icon='droplet', color='blue')
    deleted = _row(2, deleted_at=datetime(2026, 10, 6, tzinfo=timezone.utc))
    data = {
        active.id: RoutineRecord(
            versions=((TODAY - timedelta(days=6), ALL),), end=None,
            completed=frozenset({TODAY - timedelta(days=3), TODAY - timedelta(days=1)}),
            skips=frozenset({TODAY - timedelta(days=2), TODAY}),
        ),
        deleted.id: RoutineRecord(
            versions=((TODAY - timedelta(days=6), ALL),), end=TODAY - timedelta(days=2),
            completed=frozenset({TODAY - timedelta(days=4)}),
        ),
    }

    async def _all(session, uid):
        return [active, deleted]

    async def _records(session, profile, rows, until):
        assert until == TODAY
        return data

    monkeypatch.setattr(routine, '_all_routines', _all)
    monkeypatch.setattr(routine, '_routine_records', _records)
    return active


def test_stats_clamps_to_today_and_shapes_the_contract(client, records):
    response = client.get('/routines/stats', params={'from': '2026-10-05', 'to': '2026-10-31'})
    assert response.status_code == 200, response.text
    assert response.headers['x-app-local-date'] == '2026-10-08'
    assert response.headers['cache-control'] == 'private, no-store'
    body = response.json()
    assert body['today'] == '2026-10-08'
    assert body['days'] == [
        {'date': '2026-10-05', 'scheduled': 2, 'completed': 1},
        {'date': '2026-10-06', 'scheduled': 0, 'completed': 0},
        {'date': '2026-10-07', 'scheduled': 1, 'completed': 1},
        {'date': '2026-10-08', 'scheduled': 0, 'completed': 0},
    ]
    assert body['summary'] == {
        'current_streak': 3, 'best_streak': 3, 'perfect_days': 1, 'total_completed': 3,
        'completed_this_month': 3, 'overall_rate': 3 / 9, 'monthly_rate': 3 / 9,
    }
    assert body['routines'] == [{
        'id': str(records.id), 'name': 'Drink water', 'icon': 'droplet', 'color': 'blue',
        'scheduled_dates': ['2026-10-05', '2026-10-07'],
        'completed_dates': ['2026-10-05', '2026-10-07'],
        'skipped_dates': ['2026-10-06', '2026-10-08'],
        'current_streak': 2, 'best_streak': 2, 'total_completed': 2, 'completed_this_month': 2,
        'overall_rate': 2 / 5,
    }]


def test_stats_range_entirely_after_today_has_no_days(client, records):
    response = client.get('/routines/stats', params={'from': '2026-10-09', 'to': '2026-10-31'})
    assert response.status_code == 200, response.text
    assert response.json()['days'] == []
    assert response.json()['routines'][0]['scheduled_dates'] == []


@pytest.mark.parametrize('params', [
    {'from': '2026-10-08', 'to': '2026-10-07'},
    {'from': '2025-10-01', 'to': '2026-10-07'},
    {'from': '2026-10-01'},
    {'to': '2026-10-01'},
    {'from': '2026-10-1', 'to': '2026-10-08'},
])
def test_stats_rejects_reversed_or_too_long_or_malformed_ranges(client, records, params):
    response = client.get('/routines/stats', params=params)
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'VALIDATION'


def test_stats_accepts_the_longest_range(client, records):
    start = date(2026, 10, 8) - timedelta(days=370)
    response = client.get('/routines/stats', params={'from': start.isoformat(), 'to': '2026-10-08'})
    assert response.status_code == 200, response.text
    assert len(response.json()['days']) == 371


@pytest.mark.parametrize('body', [
    {'template_ids': ['make_bed', 'make_bed']},
    {'template_ids': [f't{i}' for i in range(31)]},
    {'template_ids': ['Make Bed']},
    {},
])
def test_template_selection_validates_before_the_service(client, monkeypatch, body):
    async def unexpected(*args):
        raise AssertionError('service must not run')

    monkeypatch.setattr(routine, 'select_templates', unexpected)
    response = client.post('/routine-templates/selection', json=body)
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'VALIDATION'


@pytest.mark.parametrize('body', [
    {'name': '산책', 'days_of_week': [1], 'color': 'red'},
    {'name': '산책', 'days_of_week': [1], 'icon': 'Bed'},
])
def test_create_rejects_unknown_color_and_icon_format(client, body):
    response = client.post('/routines', json=body)
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'VALIDATION'


@pytest.mark.parametrize('method', ['post', 'delete'])
def test_skip_routes_use_the_request_day(client, monkeypatch, method):
    calls = []

    async def record(session, user_id, routine_id, day):
        calls.append((user_id, routine_id, day.local_date))

    monkeypatch.setattr(routine, 'skip' if method == 'post' else 'unskip', record)
    response = getattr(client, method)('/routines/abc/skip')
    assert response.status_code == 204
    assert response.headers['x-app-local-date'] == '2026-10-08'
    assert calls == [(str(UID), 'abc', TODAY)]
