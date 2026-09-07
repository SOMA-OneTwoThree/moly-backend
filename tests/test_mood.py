"""감정일기의 날짜 경계, 사용자 범위, 원자 저장 및 HTTP 계약."""
import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.core.db import get_session
from app.core.errors import AppError
from app.core.security import get_current_user
from app.main import app
from app.schemas.mood import MoodPutRequest
from app.services import mood, privacy

UID = "11111111-1111-1111-1111-111111111111"
OTHER_UID = "22222222-2222-2222-2222-222222222222"


class FakeSession:
    def __init__(self, rows=()):
        self.rows = rows
        self.statements = []
        self.commits = 0

    async def execute(self, stmt):
        self.statements.append(stmt)
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: self.rows),
            scalar_one=lambda: self.rows[0],
        )

    async def commit(self):
        self.commits += 1


def compiled(stmt):
    return stmt.compile(dialect=postgresql.dialect())


@pytest.mark.parametrize("month,last_day", [
    ("2024-02", 29), ("2025-02", 28), ("2026-12", 31),
    ("2027-01", 31), ("0001-01", 31), ("9999-12", 31),
])
async def test_month_boundaries_and_user_scope(month, last_day):
    session = FakeSession()
    assert await mood.list_moods(session, UID, month) == {"data": []}
    sql = compiled(session.statements[0])
    year, number = map(int, month.split("-"))
    assert sql.params == {
        "user_id_1": uuid.UUID(UID),
        "entry_date_1": date(year, number, 1),
        "entry_date_2": date(year, number, last_day),
    }
    assert "mood_entries.user_id =" in str(sql)
    assert "mood_entries.entry_date >=" in str(sql)
    assert "mood_entries.entry_date <=" in str(sql)
    assert "ORDER BY mood_entries.entry_date DESC" in str(sql)
    assert len(session.statements) == 1
    assert session.commits == 0  # 조회가 기록을 자동 생성하지 않는다.


@pytest.mark.parametrize("user_id", [UID, OTHER_UID])
async def test_put_uses_atomic_user_date_upsert_and_preserves_values(user_id):
    note = "  오늘의 기록\n" * 2000
    row = SimpleNamespace(entry_date=date(2024, 2, 29), kind="new-client-kind", note=note)
    session = FakeSession([row])
    out = await mood.put_mood(
        session, user_id, "2024-02-29", MoodPutRequest(kind=row.kind, note=note)
    )
    assert out == {"date": date(2024, 2, 29), "kind": row.kind, "note": note}
    assert session.commits == 1
    assert len(session.statements) == 1
    sql = compiled(session.statements[0])
    assert sql.params["user_id"] == uuid.UUID(user_id)
    assert sql.params["entry_date"] == date(2024, 2, 29)
    assert sql.params["kind"] == "new-client-kind"
    assert sql.params["note"] == note
    assert "ON CONFLICT (user_id, entry_date) DO UPDATE" in str(sql)
    assert "kind = excluded.kind" in str(sql)
    assert "note = excluded.note" in str(sql)
    assert "updated_at = now()" in str(sql)


async def test_delete_is_idempotent_and_scoped():
    session = FakeSession()
    for _ in range(2):
        await mood.delete_mood(session, OTHER_UID, "2030-12-31")
    assert session.commits == 2
    for stmt in session.statements:
        sql = compiled(stmt)
        assert "DELETE FROM mood_entries WHERE mood_entries.user_id =" in str(sql)
        assert "AND mood_entries.entry_date =" in str(sql)
        assert sql.params == {"user_id_1": uuid.UUID(OTHER_UID), "entry_date_1": date(2030, 12, 31)}


@pytest.mark.parametrize("operation", ["list", "put", "delete"])
async def test_privacy_barrier_precedes_data_access(monkeypatch, operation):
    barrier = AsyncMock(side_effect=AppError("ACCOUNT_DELETING", 409, "blocked"))
    monkeypatch.setattr(privacy, "ensure_subject_active", barrier)
    session = FakeSession()
    with pytest.raises(AppError):
        if operation == "list":
            await mood.list_moods(session, UID, "2026-09")
        elif operation == "put":
            await mood.put_mood(session, UID, "2026-09-07", MoodPutRequest(kind="neutral"))
        else:
            await mood.delete_mood(session, UID, "2026-09-07")
    barrier.assert_awaited_once_with(session, uuid.UUID(UID))
    assert session.statements == []
    assert session.commits == 0


@pytest.fixture
def client():
    session = FakeSession([
        SimpleNamespace(entry_date=date(2026, 9, 7), kind="neutral", note="")
    ])

    async def session_override():
        yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_current_user] = lambda: UID
    try:
        yield TestClient(app), session
    finally:
        app.dependency_overrides.clear()


def test_http_read_save_default_note_and_delete(client):
    http, session = client
    item = {"date": "2026-09-07", "kind": "neutral", "note": ""}
    response = http.put("/moods/2026-09-07", json={"kind": "neutral"})
    assert response.status_code == 200
    assert response.json() == item
    assert compiled(session.statements[-1]).params["note"] == ""
    assert http.get("/moods?month=2026-09").json() == {"data": [item]}
    response = http.delete("/moods/2026-09-07")
    assert response.status_code == 204
    assert response.content == b""


@pytest.mark.parametrize("method,url,body", [
    ("get", "/moods", None),
    ("get", "/moods?month=2026-13", None),
    ("get", "/moods?month=2026-2", None),
    ("get", "/moods?month=0000-01", None),
    ("put", "/moods/2025-02-29", {"kind": "neutral"}),
    ("put", "/moods/20260907", {"kind": "neutral"}),
    ("put", "/moods/2026-09-07T00:00:00", {"kind": "neutral"}),
    ("put", "/moods/2026-09-07", {}),
    ("put", "/moods/2026-09-07", {"kind": "neutral", "note": None}),
    ("delete", "/moods/2026-02-30", None),
])
def test_invalid_inputs_use_validation_envelope(client, method, url, body):
    http, session = client
    response = http.request(method, url, json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION"
    assert session.statements == []


@pytest.mark.parametrize("method,url,body", [
    ("get", "/moods?month=2026-09", None),
    ("put", "/moods/2026-09-07", {"kind": "neutral"}),
    ("delete", "/moods/2026-09-07", None),
])
def test_all_routes_require_auth(client, method, url, body):
    http, session = client
    del app.dependency_overrides[get_current_user]
    response = http.request(method, url, json=body)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert session.statements == []
