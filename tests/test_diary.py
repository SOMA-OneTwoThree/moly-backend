"""diary 서비스 — 목록·상세·열람·타입매핑·인증(DB mock)."""
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.errors import AppError
from app.main import app
from app.services import diary as diary_service

UID = "11111111-1111-1111-1111-111111111111"
UID_UUID = uuid.UUID(UID)
PAST = datetime.now(timezone.utc) - timedelta(hours=1)


class _Scalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return list(self._items)


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return _Scalars(self._items)


class FakeSession:
    def __init__(self, rows=None, get_obj=None):
        self.rows = rows or []
        self.get_obj = get_obj
        self.committed = False

    async def execute(self, stmt):
        return _Result(self.rows)

    async def get(self, model, key):
        return self.get_obj

    async def commit(self):
        self.committed = True


def _diary(**over):
    base = dict(
        id=uuid.uuid4(), user_id=UID_UUID, diary_date=date(2026, 7, 5), source="llm",
        weather="cloudy", content="오늘 지우는 회의 얘기를 한참 했다. " * 5,
        published_at=PAST, first_read_at=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_type_mapping():
    assert diary_service._type("llm") == "personal"
    assert diary_service._type("preset") == "moly"


# --- 웰컴 일기 ---
def test_welcome_content_title_josa_and_nickname_swap():
    # 받침 있음 → 과, 없음 → 와. 닉네임이 제목·본문 모두 교체.
    seungmin = diary_service._welcome_content("승민")
    assert seungmin.startswith("승민과의 만남\n\n")
    assert "이름은 승민." in seungmin
    assert diary_service._welcome_content("지호").startswith("지호와의 만남")
    assert "사용자" not in seungmin  # 화자 라벨 누출 없음


def test_welcome_date_is_signup_activity_date_minus_one():
    # 웰컴 = 가입 activity_date - 1. 주간 가입(로컬 04시 이후)은 activity_date=가입 로컬일.
    created = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)  # = KST 7/15 11:00 → activity_date 7/15
    assert diary_service._welcome_date(created, "Asia/Seoul") == date(2026, 7, 14)


def test_welcome_date_uses_activity_boundary_for_early_morning_signup():
    # SOMA-287 회귀: 00~04시(로컬) 가입은 activity_date가 전날 → 웰컴은 그보다 하루 앞.
    # 첫 대화 activity_date(=7/14)와 웰컴 슬롯(7/13)이 겹치지 않아야 개인일기가 안 스킵된다.
    created = datetime(2026, 7, 14, 17, 0, tzinfo=timezone.utc)  # = KST 7/15 02:00 → activity_date 7/14
    assert diary_service._welcome_date(created, "Asia/Seoul") == date(2026, 7, 13)
    # 음수 오프셋 타임존(PDT, UTC-7)도 동일: 로컬 02:00 → activity_date 7/14 → 웰컴 7/13.
    la = datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc)  # = LA 7/15 02:00
    assert diary_service._welcome_date(la, "America/Los_Angeles") == date(2026, 7, 13)


async def test_ensure_welcome_skips_without_nickname():
    profile = SimpleNamespace(nickname=None, created_at=PAST, timezone="Asia/Seoul")
    session = FakeSession(get_obj=profile)
    await diary_service.ensure_welcome(session, UID)
    assert session.committed is False  # 닉네임 없으면 생성 안 함(온보딩 후 다음 조회에서)


async def test_ensure_welcome_inserts_when_onboarded():
    profile = SimpleNamespace(nickname="승민", created_at=PAST, timezone="Asia/Seoul")
    session = FakeSession(get_obj=profile)
    await diary_service.ensure_welcome(session, UID)
    assert session.committed is True  # 삽입 시도 + 커밋(멱등은 DB ON CONFLICT가 담당)


def test_list_item_welcome_exposes_title_and_strips_body():
    d = _diary(source="welcome", content="승민과의 만남\n\n오늘은 새 친구를 만났다.")
    item = diary_service._list_item(d)
    assert item["title"] == "승민과의 만남"
    assert item["type"] == "moly"
    assert item["preview"] == "오늘은 새 친구를 만났다."  # 제목 줄은 프리뷰에서 분리


def test_list_item_non_welcome_has_null_title():
    item = diary_service._list_item(_diary(source="llm"))
    assert item["title"] is None
    assert item["preview"].startswith("오늘 지우")  # 본문 그대로


async def test_get_diary_welcome_exposes_title_and_strips_body():
    d = _diary(source="welcome", content="승민과의 만남\n\n오늘은 새 친구를 만났다.")
    out = await diary_service.get_diary(FakeSession(get_obj=d), UID, str(d.id))
    assert out["title"] == "승민과의 만남"
    assert out["body"] == "오늘은 새 친구를 만났다."
    assert out["conversation_ref"] is None  # 웰컴은 개인일기 아님


async def test_list_diaries_shape_and_cursor():
    rows = [_diary(diary_date=date(2026, 7, d)) for d in (7, 6, 5)]  # 3건
    out = await diary_service.list_diaries(FakeSession(rows=rows), UID, limit=2)
    assert len(out["data"]) == 2  # limit 적용
    assert out["data"][0]["type"] == "personal"
    assert len(out["data"][0]["preview"]) <= 60
    assert out["data"][0]["read"] is False
    assert out["next_cursor"] == "2026-07-06"  # 다음 페이지 있음(3>2)


async def test_list_diaries_no_next_when_exhausted():
    out = await diary_service.list_diaries(FakeSession(rows=[_diary()]), UID, limit=30)
    assert out["next_cursor"] is None


async def test_get_diary_personal_has_conversation_ref():
    d = _diary(source="llm", diary_date=date(2026, 7, 5))
    out = await diary_service.get_diary(FakeSession(get_obj=d), UID, str(d.id))
    assert out["type"] == "personal"
    assert out["conversation_ref"] == {"anchor_date": "2026-07-05"}
    assert out["body"].startswith("오늘 지우")


async def test_get_diary_moly_has_no_conversation_ref():
    d = _diary(source="preset", content="캐피는 오늘도 뒹굴거렸다.")
    out = await diary_service.get_diary(FakeSession(get_obj=d), UID, str(d.id))
    assert out["type"] == "moly"
    assert out["conversation_ref"] is None


async def test_get_diary_not_owned_404():
    d = _diary(user_id=uuid.uuid4())  # 다른 유저
    with pytest.raises(AppError) as e:
        await diary_service.get_diary(FakeSession(get_obj=d), UID, str(d.id))
    assert e.value.http_status == 404


async def test_get_diary_unpublished_404():
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    d = _diary(published_at=future)
    with pytest.raises(AppError) as e:
        await diary_service.get_diary(FakeSession(get_obj=d), UID, str(d.id))
    assert e.value.http_status == 404


async def test_mark_read_sets_first_read():
    d = _diary(first_read_at=None)
    session = FakeSession(get_obj=d)
    await diary_service.mark_read(session, UID, str(d.id))
    assert d.first_read_at is not None
    assert session.committed is True


async def test_mark_read_idempotent_when_already_read():
    d = _diary(first_read_at=PAST)
    session = FakeSession(get_obj=d)
    await diary_service.mark_read(session, UID, str(d.id))
    assert session.committed is False  # 이미 읽음 → 재기록/커밋 안 함


# --- 인증 ---
async def _dummy_session():
    yield None


def test_diaries_requires_auth():
    app.dependency_overrides[get_session] = _dummy_session
    try:
        r = TestClient(app).get("/diaries")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"
