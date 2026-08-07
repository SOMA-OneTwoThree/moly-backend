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
from app.services import naming

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
    def __init__(self, rows=None, get_obj=None, welcome_id=None):
        self.rows = rows or []
        self.get_obj = get_obj
        self.welcome_id = welcome_id
        self.committed = False
        self.executed = False

    async def execute(self, stmt):
        self.executed = True
        return _Result(self.rows)

    async def scalar(self, stmt):
        return self.welcome_id

    async def get(self, model, key):
        return self.get_obj

    async def commit(self):
        self.committed = True


class SequentialScalarSession(FakeSession):
    def __init__(self, values, **kwargs):
        super().__init__(**kwargs)
        self.values = iter(values)

    async def scalar(self, stmt):
        return next(self.values)


def _diary(**over):
    base = dict(
        id=uuid.uuid4(), user_id=UID_UUID, diary_date=date(2026, 7, 5), source="llm",
        weather="cloudy", content="오늘 지우는 회의 얘기를 한참 했다. " * 5,
        published_at=PAST, first_read_at=None,
        # get_diary가 같은 FakeSession.get으로 Profile도 읽으므로 nickname 속성을 함께 제공.
        nickname="승민",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_type_mapping():
    assert diary_service._type("llm") == "personal"
    assert diary_service._type("preset") == "moly"


# --- 웰컴 일기 ---
def test_welcome_content_is_placeholder_and_renders_with_josa():
    # 저장분은 placeholder(이름 표면 0), 렌더 시 받침에 맞는 조사(과/와)로 이름 교체.
    tpl = diary_service._WELCOME_CONTENT
    assert naming.TOKEN in tpl and "승민" not in tpl  # 저장은 토큰만
    seungmin = naming.render(tpl, "승민")
    assert seungmin.startswith("승민, 첫 만남\n\n")
    assert "오늘 승민과 처음 대화를 나눴다." in seungmin
    assert naming.render(tpl, "지호").startswith("지호, 첫 만남")
    assert "사용자" not in seungmin  # 화자 라벨 누출 없음


def test_welcome_date_is_first_conversation_local_date():
    created = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)  # = KST 7/15 11:00 → activity_date 7/15
    assert diary_service._welcome_date(created, "Asia/Seoul") == date(2026, 7, 15)


def test_welcome_date_does_not_use_the_four_am_activity_boundary():
    created = datetime(2026, 7, 14, 17, 0, tzinfo=timezone.utc)  # = KST 7/15 02:00 → activity_date 7/14
    assert diary_service._welcome_date(created, "Asia/Seoul") == date(2026, 7, 15)
    la = datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc)  # = LA 7/15 02:00
    assert diary_service._welcome_date(la, "America/Los_Angeles") == date(2026, 7, 15)


def test_welcome_is_not_created_by_a_list_read_anymore():
    assert not hasattr(diary_service, "ensure_welcome")


async def test_first_committed_turn_welcome_joins_the_callers_transaction(monkeypatch):
    diary_id = uuid.uuid4()
    profile = SimpleNamespace(
        id=UID_UUID,
        nickname=None,
        language="ko",
        timezone="Asia/Seoul",
        relationship_started_at=None,
    )
    session = FakeSession(get_obj=profile, welcome_id=diary_id)

    async def _sources(*args, **kwargs):
        return None

    async def _document(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.diary_recall_repo.record_diary_sources", _sources)
    monkeypatch.setattr("app.services.diary_recall_repo.upsert_diary_recall_document", _document)
    inserted = await diary_service.ensure_welcome_for_first_committed_turn(
        session, profile, PAST, source_message_id=11
    )
    assert inserted == diary_id
    assert session.committed is False
    assert profile.relationship_started_at == PAST


async def test_existing_welcome_is_a_true_noop_for_projection_hooks(monkeypatch):
    diary_id = uuid.uuid4()
    profile = SimpleNamespace(
        id=UID_UUID,
        language="ko",
        timezone="Asia/Seoul",
        relationship_started_timezone="America/Los_Angeles",
        relationship_started_at=PAST,
    )
    session = SequentialScalarSession([None, diary_id], get_obj=profile)
    calls = []

    async def _called(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr("app.services.diary_recall_repo.record_diary_sources", _called)
    monkeypatch.setattr("app.services.diary_recall_repo.upsert_diary_recall_document", _called)
    inserted = await diary_service.ensure_welcome_for_first_committed_turn(
        session, profile, PAST, source_message_id=None
    )
    assert inserted is None
    assert calls == []


async def test_missing_welcome_for_existing_relationship_uses_first_user_message(monkeypatch):
    diary_id = uuid.uuid4()
    profile = SimpleNamespace(
        id=UID_UUID,
        language="ko",
        timezone="Asia/Seoul",
        relationship_started_timezone="America/Los_Angeles",
        relationship_started_at=PAST,
    )
    session = SequentialScalarSession([diary_id, 7], get_obj=profile)
    observed = {}

    async def _sources(*args, **kwargs):
        observed["sources"] = kwargs

    async def _document(*args, **kwargs):
        observed["document"] = kwargs

    monkeypatch.setattr("app.services.diary_recall_repo.record_diary_sources", _sources)
    monkeypatch.setattr("app.services.diary_recall_repo.upsert_diary_recall_document", _document)
    inserted = await diary_service.ensure_welcome_for_first_committed_turn(
        session, profile, PAST, source_message_id=None
    )
    assert inserted == diary_id
    assert observed["sources"]["message_ids"] == [7]
    assert observed["document"]["diary_id"] == diary_id


def test_list_item_welcome_exposes_title_and_strips_body():
    # placeholder 저장분을 현재 닉네임으로 렌더 → 제목/프리뷰에 이름 반영.
    d = _diary(source="welcome", content=f"{naming.TOKEN}, 첫 만남\n\n오늘은 새 친구를 만났다.")
    item = diary_service._list_item(d, "승민")
    assert item["title"] == "승민, 첫 만남"
    assert item["type"] == "moly"
    assert item["preview"] == "오늘은 새 친구를 만났다."  # 제목 줄은 프리뷰에서 분리


def test_list_item_non_welcome_has_null_title():
    item = diary_service._list_item(_diary(source="llm"), "승민")
    assert item["title"] is None
    assert item["preview"].startswith("오늘 지우")  # 본문 그대로


async def test_get_diary_welcome_exposes_title_and_strips_body():
    # placeholder 저장분 → egress에서 현재 닉네임(승민)으로 렌더.
    d = _diary(source="welcome", content=f"{naming.TOKEN}, 첫 만남\n\n오늘은 새 친구를 만났다.")
    out = await diary_service.get_diary(FakeSession(get_obj=d), UID, str(d.id))
    assert out["title"] == "승민, 첫 만남"
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


@pytest.mark.parametrize(
    "state",
    [
        {"record_status": "deleted", "deleted_at": PAST},
        {"record_status": "published", "deleted_at": PAST},
    ],
)
async def test_get_diary_never_serves_deleted_records(state):
    d = _diary(**state)
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
