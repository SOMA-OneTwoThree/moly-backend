"""API 전이·오류·시간대·멱등 — 실제 라우터와 서비스를 메모리 세션 위에서 돌린다.

실제 Postgres의 제약·잠금은 tests/integration/test_daily_practice_local.py가 검증한다.
"""
import asyncio
import random
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.errors import AppError
from app.core.security import get_current_user
from app.main import app
from app.models.daily_practice import DailyPracticeAction, DailyPracticeOffer
from app.models.mood import MoodEntry
from app.models.routine import Routine, RoutineCompletion, RoutineSchedule, RoutineSkip
from app.services import daily_practice, jobs, llm, privacy
from tests.fake_db import FakeDbSession

USER = "00000000-0000-0000-0000-000000000001"
OTHER = uuid.UUID("00000000-0000-0000-0000-000000000002")
SEOUL = "Asia/Seoul"
LA = "America/Los_Angeles"
NOON_SEOUL = datetime(2026, 11, 1, 12, 0, tzinfo=ZoneInfo(SEOUL)).astimezone(timezone.utc)
HEADERS = {"X-App-Timezone": SEOUL, "X-App-Locale": "ko"}
TABLES = (DailyPracticeAction, DailyPracticeOffer, MoodEntry, Routine, RoutineSchedule,
          RoutineCompletion, RoutineSkip)


def action(n, category="rest", slots=("morning", "day", "evening", "night"), status="active"):
    return SimpleNamespace(
        id=uuid.UUID(int=n), category=category, slots=list(slots), icon="droplet", status=status,
        title_ko=f"물 {n}잔 마시기", title_en=f"Drink water {n}", title_ja=f"お水{n}杯飲む",
        speech_ko=f"물 {n}잔 어때?", speech_en=f"Water {n}?", speech_ja=f"お水{n}杯どう?",
    )


class Session(FakeDbSession):
    """FakeDbSession에 쓰기를 더한 세션. 하루 한 행 제약을 commit 전에 검사한다."""

    def __init__(self, tables, clock):
        self.tables = tables
        self.statements = []
        self.rolled_back = False
        self.clock = clock
        self.commits = 0

    def add(self, row):
        if getattr(row, "created_at", None) is None:
            row.created_at = self.clock.now + timedelta(microseconds=len(self.tables[type(row)]))
        self.tables[type(row)].append(row)

    async def get(self, model, key):
        return next((row for row in self.tables[model] if row.id == key), None)

    async def flush(self):
        live = [(r.user_id, r.local_date) for r in self.tables[DailyPracticeOffer] if r.status != "skipped"]
        assert len(live) == len(set(live)), "daily_practice_offers_one_live_uq 위반"

    async def commit(self):
        await self.flush()
        self.commits += 1


@pytest.fixture
def clock():
    return SimpleNamespace(now=NOON_SEOUL)


@pytest.fixture
def tables():
    return {model: [] for model in TABLES}


@pytest.fixture
def pool(tables):
    tables[DailyPracticeAction].extend(
        action(i * 10 + j + 1, category)
        for i, category in enumerate(("rest", "body", "calm", "mind")) for j in range(3)
    )
    return tables[DailyPracticeAction]


@pytest.fixture
def enqueue(monkeypatch):
    mock = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(jobs, "enqueue", mock)
    return mock


@pytest.fixture
def client(monkeypatch, tables, clock, enqueue):
    session = Session(tables, clock)

    async def session_dep():
        yield session

    async def profile(_session, _user_id):
        return SimpleNamespace(timezone=SEOUL)

    async def no_lock(_session, _uid):
        return None

    async def no_llm(*_args, **_kwargs):
        raise AssertionError("사용자 요청은 LLM을 부르지 않는다")

    monkeypatch.setattr(daily_practice, "_load_profile", profile)
    monkeypatch.setattr(daily_practice, "advisory_xact_lock", no_lock)
    monkeypatch.setattr(daily_practice, "_clock", lambda _now=None: clock.now)
    monkeypatch.setattr(llm, "generate", no_llm)
    app.dependency_overrides[get_session] = session_dep
    app.dependency_overrides[get_current_user] = lambda: USER
    yield TestClient(app)
    app.dependency_overrides.clear()


def offer(client, **kwargs):
    response = client.post("/daily-practice/offer", headers=HEADERS, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def act(client, verb, practice_id, headers=HEADERS, **kwargs):
    return client.post(f"/daily-practice/{practice_id}/{verb}", headers=headers, **kwargs)


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def code(response):
    return response.status_code, response.json()["error"]["code"]


def accepted(client):
    practice_id = offer(client)["practice"]["id"]
    ok(act(client, "accept", practice_id))
    return practice_id


def completed(client):
    practice_id = accepted(client)
    ok(act(client, "complete", practice_id))
    return practice_id


# ── 조회 ──────────────────────────────────────────────────────────

def test_get_is_read_only_and_reports_no_offer(client, pool, tables):
    response = client.get("/daily-practice", headers=HEADERS)
    assert response.json() == {
        "local_date": "2026-11-01",
        "expires_at": "2026-11-01T15:00:00+00:00",
        "locale": "ko",
        "practice": None,
        "offers_remaining": 2,
    }
    assert response.headers["cache-control"] == "private, no-store"
    assert tables[DailyPracticeOffer] == []


def test_get_returns_the_current_row_after_an_offer(client, pool):
    made = offer(client)
    assert ok(client.get("/daily-practice", headers=HEADERS)) == made


@pytest.mark.parametrize(("header", "locale", "title"), [
    ("ko-KR", "ko", "물 {}잔 마시기"), ("en", "en", "Drink water {}"), ("ja-JP", "ja", "お水{}杯飲む"),
    (None, "en", "Drink water {}"), ("zh-Hant-TW", "en", "Drink water {}"),
])
def test_offer_is_returned_in_the_requested_language(client, pool, header, locale, title):
    headers = {"X-App-Timezone": SEOUL} | ({"X-App-Locale": header} if header else {})
    body = ok(client.post("/daily-practice/offer", headers=headers))
    number = next(
        a.id.int for a in pool if getattr(a, f"title_{locale}") == body["practice"]["title"]
    )
    assert body["locale"] == locale
    assert body["practice"]["title"] == title.format(number)


# ── 제안 ─────────────────────────────────────────────────────────

def test_offer_creates_one_offer_with_the_contract_shape(client, pool, tables, enqueue):
    body = offer(client)
    practice = body["practice"]
    assert set(practice) == {"id", "status", "category", "title", "speech", "icon", "routine_added"}
    assert practice["status"] == "offered" and practice["routine_added"] is False
    assert practice["icon"] == "droplet"
    assert body["offers_remaining"] == 2
    assert len(tables[DailyPracticeOffer]) == 1
    assert uuid.UUID(practice["id"]) == tables[DailyPracticeOffer][0].id
    enqueue.assert_not_called()


@pytest.mark.parametrize("prepare", [lambda c: None, accepted, completed])
def test_offer_returns_the_current_row_whatever_its_status(client, pool, tables, prepare):
    prepare(client)
    first = offer(client)
    assert offer(client) == first
    assert len(tables[DailyPracticeOffer]) == 1


def test_offer_body_is_optional_and_empty_object_is_fine(client, pool):
    assert client.post("/daily-practice/offer", headers=HEADERS).status_code == 200
    assert client.post("/daily-practice/offer", headers=HEADERS, json={}).status_code == 200


@pytest.mark.parametrize("body", [
    {"focus": ["rest", "body", "calm", "mind"]},
    {"focus": ["sleep"]},
    {"focus": "rest"},
    {"focus": [], "extra": 1},
])
@pytest.mark.parametrize("path", ["offer", f"{uuid.UUID(int=99)}/skip"])
def test_invalid_offer_bodies_are_rejected(client, pool, body, path):
    response = client.post(f"/daily-practice/{path}", headers=HEADERS, json=body)
    assert code(response) == (422, "VALIDATION")


def test_focus_steers_selection_and_is_not_persisted(client, pool, tables, enqueue):
    body = offer(client, json={"focus": ["mind", "calm"]})
    assert body["practice"]["category"] == "mind"
    columns = set(DailyPracticeOffer.__table__.columns.keys())
    assert columns == {"id", "user_id", "action_id", "local_date", "expires_at", "status",
                       "routine_id", "created_at", "updated_at"}
    enqueue.assert_not_called()


def test_recent_low_mood_is_read_without_the_note(client, pool, tables):
    tables[MoodEntry].append(SimpleNamespace(
        user_id=uuid.UUID(USER), entry_date=date(2026, 10, 31), kind="tired", note="never read"
    ))
    results = set()
    for _ in range(8):
        results.add(offer(client)["practice"]["category"])
        tables[DailyPracticeOffer].clear()
    assert results <= {"rest", "calm"}


def test_mood_older_than_three_days_is_ignored(client, monkeypatch, pool, tables):
    tables[MoodEntry].append(SimpleNamespace(
        user_id=uuid.UUID(USER), entry_date=date(2026, 10, 29), kind="tired", note=""
    ))
    captured = {}
    real = daily_practice.selection.choose

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(daily_practice.selection, "choose", spy)
    offer(client)
    assert list(captured["moods"]) == []


def test_another_users_mood_and_history_are_not_used(client, monkeypatch, pool, tables):
    other_row = SimpleNamespace(
        id=uuid.uuid4(), user_id=OTHER, action_id=pool[0].id, local_date=date(2026, 11, 1),
        expires_at=NOON_SEOUL + timedelta(hours=3), status="skipped", routine_id=None,
        created_at=NOON_SEOUL,
    )
    tables[DailyPracticeOffer].append(other_row)
    tables[MoodEntry].append(SimpleNamespace(
        user_id=OTHER, entry_date=date(2026, 11, 1), kind="tired", note=""
    ))
    captured = {}
    real = daily_practice.selection.choose

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(daily_practice.selection, "choose", spy)
    offer(client)
    assert captured["history"] == [] and list(captured["moods"]) == []


def test_empty_pool_is_200_with_null_practice_and_requests_top_up(client, tables, enqueue):
    body = offer(client, json={"focus": ["calm"]})
    assert body["practice"] is None and body["offers_remaining"] == 2
    assert tables[DailyPracticeOffer] == []
    kwargs = enqueue.call_args.kwargs
    assert kwargs["job_type"] == daily_practice.JOB_TOP_UP
    assert kwargs["dedup_key"] == f"{daily_practice.JOB_TOP_UP}:calm:2026-11-01"
    assert kwargs["payload"] == {"category": "calm", "slot": "day"}
    assert kwargs.get("user_id") is None


def test_time_slot_filter_applies_to_the_local_hour(client, tables, clock):
    tables[DailyPracticeAction].extend([action(1, slots=("morning",)), action(2, slots=("night",))])
    clock.now = datetime(2026, 11, 1, 23, 0, tzinfo=ZoneInfo(SEOUL)).astimezone(timezone.utc)
    assert offer(client)["practice"]["title"] == "물 2잔 마시기"


def test_retired_actions_are_not_offered_but_a_row_keeps_showing_them(client, tables):
    tables[DailyPracticeAction].extend([action(1), action(2, status="retired")])
    body = offer(client)
    assert body["practice"]["title"] == "물 1잔 마시기"
    tables[DailyPracticeAction][0].status = "retired"
    assert ok(client.get("/daily-practice", headers=HEADERS))["practice"]["title"] == "물 1잔 마시기"


def test_top_up_failure_never_affects_the_response(client, tables, enqueue):
    enqueue.side_effect = RuntimeError("queue down")
    body = offer(client)
    assert body["practice"] is None


def test_recently_shown_actions_are_excluded_from_the_next_days(client, tables, clock):
    tables[DailyPracticeAction].extend([action(1), action(2)])
    first = offer(client)["practice"]["title"]
    clock.now += timedelta(days=1)
    assert offer(client)["practice"]["title"] != first


def test_relaxing_the_30_days_requests_a_top_up(client, tables, clock, enqueue):
    tables[DailyPracticeAction].append(action(1))
    assert offer(client)["practice"] is not None
    enqueue.assert_not_called()
    clock.now += timedelta(days=1)
    assert offer(client)["practice"] is not None
    enqueue.assert_awaited_once()


# ── 건너뛰기 ──────────────────────────────────────────────────────

def test_skip_records_and_returns_the_next_offer_until_the_limit(client, pool, tables):
    first = offer(client)
    second = ok(act(client, "skip", first["practice"]["id"]))
    assert second["practice"]["id"] != first["practice"]["id"]
    assert second["offers_remaining"] == 1
    third = ok(act(client, "skip", second["practice"]["id"]))
    assert third["offers_remaining"] == 0
    assert code(act(client, "skip", third["practice"]["id"])) == (409, "DAILY_PRACTICE_OFFER_LIMIT")
    assert [row.status for row in tables[DailyPracticeOffer]] == ["skipped", "skipped", "offered"]
    assert ok(client.get("/daily-practice", headers=HEADERS)) == third
    assert len({row.action_id for row in tables[DailyPracticeOffer]}) == 3


def test_skip_avoids_the_skipped_category_for_the_day(client, pool, tables):
    first = offer(client, json={"focus": ["calm"]})
    assert first["practice"]["category"] == "calm"
    second = ok(act(client, "skip", first["practice"]["id"], json={"focus": ["calm"]}))
    assert second["practice"]["category"] != "calm"
    third = ok(act(client, "skip", second["practice"]["id"], json={"focus": ["calm"]}))
    assert third["practice"]["category"] not in {"calm", second["practice"]["category"]}


def test_skip_body_is_optional(client, pool):
    assert act(client, "skip", offer(client)["practice"]["id"]).status_code == 200


def test_skip_is_only_allowed_from_offered(client, pool):
    practice_id = accepted(client)
    assert code(act(client, "skip", practice_id)) == (409, "DAILY_PRACTICE_INVALID_STATE")
    ok(act(client, "complete", practice_id))
    assert code(act(client, "skip", practice_id)) == (409, "DAILY_PRACTICE_INVALID_STATE")


def test_a_skipped_row_cannot_be_skipped_again_or_accepted(client, pool):
    practice_id = offer(client)["practice"]["id"]
    ok(act(client, "skip", practice_id))
    for verb in ("skip", "accept", "complete", "uncomplete", "routine"):
        assert code(act(client, verb, practice_id)) == (409, "DAILY_PRACTICE_INVALID_STATE")


def test_skip_with_nothing_left_returns_null_and_a_later_offer_can_fill_it(client, tables, enqueue):
    tables[DailyPracticeAction].append(action(1))
    first = offer(client)
    body = ok(act(client, "skip", first["practice"]["id"]))
    assert body["practice"] is None and body["offers_remaining"] == 1
    enqueue.assert_awaited()
    tables[DailyPracticeAction].append(action(2))
    again = offer(client)
    assert again["practice"]["title"] == "물 2잔 마시기" and again["offers_remaining"] == 1


# ── 수락·완료 ─────────────────────────────────────────────────────

def test_accept_moves_to_accepted_and_is_idempotent(client, pool):
    practice_id = offer(client)["practice"]["id"]
    body = ok(act(client, "accept", practice_id))
    assert body["practice"]["status"] == "accepted"
    assert body["offers_remaining"] == 0
    assert ok(act(client, "accept", practice_id)) == body


def test_accept_is_refused_after_completion(client, pool):
    assert code(act(client, "accept", completed(client))) == (409, "DAILY_PRACTICE_INVALID_STATE")


def test_complete_requires_accepted_and_is_idempotent(client, pool):
    practice_id = offer(client)["practice"]["id"]
    assert code(act(client, "complete", practice_id)) == (409, "DAILY_PRACTICE_INVALID_STATE")
    ok(act(client, "accept", practice_id))
    body = ok(act(client, "complete", practice_id))
    assert body["practice"]["status"] == "completed"
    assert ok(act(client, "complete", practice_id)) == body


def test_uncomplete_returns_to_accepted_and_is_idempotent(client, pool):
    practice_id = completed(client)
    body = ok(act(client, "uncomplete", practice_id))
    assert body["practice"]["status"] == "accepted"
    assert ok(act(client, "uncomplete", practice_id)) == body
    assert ok(act(client, "complete", practice_id))["practice"]["status"] == "completed"


def test_uncomplete_is_refused_from_offered(client, pool):
    practice_id = offer(client)["practice"]["id"]
    assert code(act(client, "uncomplete", practice_id)) == (409, "DAILY_PRACTICE_INVALID_STATE")


def test_uncomplete_is_refused_once_a_routine_was_added(client, pool):
    practice_id = completed(client)
    ok(act(client, "routine", practice_id))
    assert code(act(client, "uncomplete", practice_id)) == (409, "DAILY_PRACTICE_INVALID_STATE")


def test_no_second_practice_after_accepting_until_the_next_day(client, pool, tables, clock):
    accepted(client)
    offer(client)
    assert len(tables[DailyPracticeOffer]) == 1
    clock.now += timedelta(days=1)
    assert offer(client)["local_date"] == "2026-11-02"
    assert len(tables[DailyPracticeOffer]) == 2


# ── 루틴 추가 ─────────────────────────────────────────────────────

def test_routine_creates_all_fields_in_one_commit(client, pool, tables):
    practice_id = completed(client)
    row = tables[DailyPracticeOffer][0]
    source = next(a for a in pool if a.id == row.action_id)
    response = ok(act(client, "routine", practice_id, headers={**HEADERS, "X-App-Locale": "ja"}))
    routine = response["routine"]
    assert routine["name"] == source.title_ja
    assert routine["icon"] == source.icon
    assert routine["color"] == daily_practice.ROUTINE_COLORS[source.category]
    assert routine["days_of_week"] == [1, 2, 3, 4, 5, 6, 7]
    assert routine["reminder_enabled"] is False and routine["reminder_time"] is None
    assert response["today"]["practice"]["routine_added"] is True
    saved = tables[Routine][0]
    assert saved.name_i18n == {"ko": source.title_ko, "en": source.title_en, "ja": source.title_ja}
    assert saved.frequency_per_week == 7
    schedule = tables[RoutineSchedule][0]
    assert schedule.routine_id == saved.id and schedule.effective_from == date(2026, 11, 1)
    assert schedule.days_of_week == [1, 2, 3, 4, 5, 6, 7]


def test_every_category_maps_to_a_valid_routine_color():
    from app.schemas.routine import RoutineColor
    from app.services.daily_practice_selection import CATEGORIES
    assert set(daily_practice.ROUTINE_COLORS) == set(CATEGORIES)
    assert set(daily_practice.ROUTINE_COLORS.values()) <= set(RoutineColor.__args__)


def test_routine_repeat_returns_the_same_routine_and_creates_nothing(client, pool, tables):
    practice_id = completed(client)
    first = ok(act(client, "routine", practice_id))
    second = ok(act(client, "routine", practice_id))
    assert second["routine"]["id"] == first["routine"]["id"]
    assert second["today"] == first["today"]
    assert len(tables[Routine]) == 1 and len(tables[RoutineSchedule]) == 1


def test_routine_repeat_reports_todays_routine_record(client, pool, tables):
    practice_id = completed(client)
    routine_id = ok(act(client, "routine", practice_id))["routine"]["id"]
    tables[RoutineCompletion].append(SimpleNamespace(
        user_id=uuid.UUID(USER), routine_id=uuid.UUID(routine_id), activity_date=date(2026, 11, 1)
    ))
    assert ok(act(client, "routine", practice_id))["routine"]["completed_today"] is True


@pytest.mark.parametrize("prepare", [
    lambda c: offer(c)["practice"]["id"],
    accepted,
])
def test_routine_is_only_allowed_from_completed(client, pool, tables, prepare):
    assert code(act(client, "routine", prepare(client))) == (409, "DAILY_PRACTICE_INVALID_STATE")
    assert tables[Routine] == []


# ── 오류 코드 ─────────────────────────────────────────────────────

VERBS = ("skip", "accept", "complete", "uncomplete", "routine")


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("practice_id", [str(uuid.UUID(int=12345)), "not-a-uuid"])
def test_unknown_id_is_404(client, pool, verb, practice_id):
    assert code(act(client, verb, practice_id)) == (404, "DAILY_PRACTICE_NOT_FOUND")


@pytest.mark.parametrize("verb", VERBS)
def test_another_users_id_is_404(client, pool, tables, verb):
    row = SimpleNamespace(
        id=uuid.uuid4(), user_id=OTHER, action_id=pool[0].id, local_date=date(2026, 11, 1),
        expires_at=NOON_SEOUL + timedelta(hours=3), status="completed", routine_id=None,
        created_at=NOON_SEOUL,
    )
    tables[DailyPracticeOffer].append(row)
    assert code(act(client, verb, row.id)) == (404, "DAILY_PRACTICE_NOT_FOUND")
    assert row.status == "completed"


@pytest.mark.parametrize("verb", VERBS)
def test_past_expiry_is_409_expired(client, pool, clock, verb):
    practice_id = completed(client)
    clock.now = datetime(2026, 11, 2, 0, 0, tzinfo=ZoneInfo(SEOUL)).astimezone(timezone.utc)
    assert code(act(client, verb, practice_id)) == (409, "DAILY_PRACTICE_EXPIRED")


@pytest.mark.parametrize("verb", ["get", "offer", *VERBS])
def test_invalid_timezone_is_422_with_no_store(client, pool, verb):
    headers = {"X-App-Timezone": "Mars/Olympus"}
    if verb == "get":
        response = client.get("/daily-practice", headers=headers)
    elif verb == "offer":
        response = client.post("/daily-practice/offer", headers=headers)
    else:
        response = act(client, verb, uuid.uuid4(), headers=headers)
    assert code(response) == (422, "APP_TIMEZONE_INVALID")
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("verb", ["get", "offer", *VERBS])
def test_account_being_deleted_is_409(client, monkeypatch, pool, tables, verb):
    async def deleting(_session, _uid):
        raise AppError("ACCOUNT_DELETING", 409, "계정 삭제를 처리하고 있어요.")

    monkeypatch.setattr(privacy, "ensure_subject_active", deleting)
    if verb == "get":
        response = client.get("/daily-practice", headers=HEADERS)
    elif verb == "offer":
        response = client.post("/daily-practice/offer", headers=HEADERS)
    else:
        response = act(client, verb, uuid.uuid4())
    assert code(response) == (409, "ACCOUNT_DELETING")
    assert response.headers["cache-control"] == "private, no-store"
    assert tables[DailyPracticeOffer] == []


@pytest.mark.parametrize(("method", "path"), [
    ("get", "/daily-practice"),
    ("post", "/daily-practice/offer"),
    *[("post", f"/daily-practice/{uuid.UUID(int=1)}/{verb}") for verb in VERBS],
])
def test_every_endpoint_requires_a_bearer_token(method, path):
    response = getattr(TestClient(app), method)(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


# ── 현지 자정과 시간대 ────────────────────────────────────────────

def test_row_disappears_exactly_at_local_midnight_and_a_new_day_starts(client, pool, tables, clock):
    first = offer(client)
    practice_id = first["practice"]["id"]
    ok(act(client, "accept", practice_id))
    midnight = datetime(2026, 11, 2, 0, 0, tzinfo=ZoneInfo(SEOUL)).astimezone(timezone.utc)

    clock.now = midnight - timedelta(seconds=1)
    still = ok(client.get("/daily-practice", headers=HEADERS))
    assert still["practice"]["id"] == practice_id and still["local_date"] == "2026-11-01"

    clock.now = midnight
    gone = ok(client.get("/daily-practice", headers=HEADERS))
    assert gone == {
        "local_date": "2026-11-02",
        "expires_at": "2026-11-02T15:00:00+00:00",
        "locale": "ko",
        "practice": None,
        "offers_remaining": 2,
    }
    next_day = offer(client)
    assert next_day["practice"]["id"] != practice_id
    assert next_day["offers_remaining"] == 2
    assert tables[DailyPracticeOffer][0].status == "accepted"


def test_expired_skips_do_not_count_toward_the_next_days_limit(client, pool, clock):
    second = ok(act(client, "skip", offer(client)["practice"]["id"]))
    ok(act(client, "skip", second["practice"]["id"]))
    clock.now += timedelta(days=1)
    assert offer(client)["offers_remaining"] == 2


def test_local_date_follows_the_request_timezone(client, pool, clock):
    clock.now = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)
    headers = {"X-App-Timezone": LA, "X-App-Locale": "en"}
    body = ok(client.post("/daily-practice/offer", headers=headers))
    assert body["local_date"] == "2026-10-31"
    assert body["expires_at"] == "2026-11-01T07:00:00+00:00"


def test_expires_at_handles_a_daylight_saving_day(client, pool, clock):
    clock.now = datetime(2026, 11, 1, 12, 0, tzinfo=ZoneInfo(LA)).astimezone(timezone.utc)
    body = ok(client.post("/daily-practice/offer", headers={"X-App-Timezone": LA}))
    assert body["local_date"] == "2026-11-01"
    assert body["expires_at"] == "2026-11-02T08:00:00+00:00"


def test_missing_timezone_header_uses_the_stored_timezone(client, pool):
    body = ok(client.post("/daily-practice/offer", headers={"X-App-Locale": "ko"}))
    assert body["local_date"] == "2026-11-01"
    assert body["expires_at"] == "2026-11-01T15:00:00+00:00"


def test_local_date_and_expiry_stay_fixed_when_the_timezone_changes(client, pool, clock):
    made = offer(client)
    clock.now += timedelta(hours=1)
    seen = ok(client.get("/daily-practice", headers={"X-App-Timezone": LA, "X-App-Locale": "ko"}))
    assert seen["local_date"] == made["local_date"]
    assert seen["expires_at"] == made["expires_at"]
    assert seen["practice"]["id"] == made["practice"]["id"]


def test_moving_west_cannot_get_a_second_practice_on_the_same_local_date(client, pool, tables, clock):
    """서울에서 받은 11/1 행이 끝난 뒤 LA로 가면 아직 11/1이다. 하루 하나 규칙을 지킨다."""
    accepted(client)
    clock.now = datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc)
    headers = {"X-App-Timezone": LA, "X-App-Locale": "ko"}
    body = ok(client.post("/daily-practice/offer", headers=headers))
    assert body["local_date"] == "2026-11-01"
    assert body["practice"] is None and body["offers_remaining"] == 0
    assert len(tables[DailyPracticeOffer]) == 1


def test_moving_east_gets_a_new_day_only_after_the_row_expires(client, pool, tables, clock):
    clock.now = datetime(2026, 11, 1, 23, 0, tzinfo=ZoneInfo(LA)).astimezone(timezone.utc)
    west = ok(client.post("/daily-practice/offer", headers={"X-App-Timezone": LA}))
    clock.now += timedelta(minutes=30)
    same = ok(client.post("/daily-practice/offer", headers=HEADERS))
    assert same["practice"]["id"] == west["practice"]["id"]
    assert same["local_date"] == "2026-11-01"
    clock.now += timedelta(hours=1)
    next_day = ok(client.post("/daily-practice/offer", headers=HEADERS))
    assert next_day["local_date"] == "2026-11-02"
    assert next_day["practice"]["id"] != west["practice"]["id"]


def test_expiry_in_the_response_is_in_the_future(client, pool, clock):
    expires = datetime.fromisoformat(offer(client)["expires_at"])
    assert expires > clock.now and expires - clock.now <= timedelta(hours=24)


# ── 동시성: 잠금을 잡은 뒤 다시 확인하는지 ───────────────────────

class _LockingSession(Session):
    """advisory lock을 사용자별 asyncio.Lock으로 흉내낸다. commit까지 유지한다."""

    def __init__(self, tables, clock, locks):
        super().__init__(tables, clock)
        self.locks = locks
        self.held = None

    async def commit(self):
        await super().commit()
        self.release()

    def release(self):
        if self.held is not None:
            self.held.release()
            self.held = None


@pytest.fixture
def concurrent(monkeypatch, tables, clock, enqueue):
    locks: dict = {}

    async def lock(session, uid):
        held = locks.setdefault(uid, asyncio.Lock())
        await held.acquire()
        session.held = held
        await asyncio.sleep(0)

    monkeypatch.setattr(daily_practice, "advisory_xact_lock", lock)
    monkeypatch.setattr(daily_practice, "_clock", lambda _now=None: clock.now)

    async def run(call):
        session = _LockingSession(tables, clock, locks)
        try:
            return await call(session)
        except AppError as error:
            return error
        finally:
            session.release()

    return run


async def test_concurrent_offers_create_one_row(concurrent, pool, tables):
    results = await asyncio.gather(*[
        concurrent(lambda s: daily_practice.offer(
            s, USER, focus=[], locale="ko", timezone_name=SEOUL, rng=random.Random(i)
        ))
        for i in range(8)
    ])
    assert len(tables[DailyPracticeOffer]) == 1
    assert len({result["practice"]["id"] for result in results}) == 1


async def test_concurrent_skips_create_exactly_one_next_offer(concurrent, pool, tables):
    first = await concurrent(lambda s: daily_practice.offer(
        s, USER, focus=[], locale="ko", timezone_name=SEOUL
    ))
    practice_id = str(first["practice"]["id"])
    results = await asyncio.gather(*[
        concurrent(lambda s: daily_practice.skip(
            s, USER, practice_id, focus=[], locale="ko", timezone_name=SEOUL
        ))
        for _ in range(4)
    ])
    assert sum(isinstance(r, dict) for r in results) == 1
    assert {r.code for r in results if isinstance(r, AppError)} == {"DAILY_PRACTICE_INVALID_STATE"}
    live = [row for row in tables[DailyPracticeOffer] if row.status != "skipped"]
    assert len(tables[DailyPracticeOffer]) == 2 and len(live) == 1


async def test_concurrent_accepts_and_skip_leave_one_accepted_row(concurrent, pool, tables):
    first = await concurrent(lambda s: daily_practice.offer(
        s, USER, focus=[], locale="ko", timezone_name=SEOUL
    ))
    practice_id = str(first["practice"]["id"])
    await asyncio.gather(
        concurrent(lambda s: daily_practice.accept(
            s, USER, practice_id, locale="ko", timezone_name=SEOUL
        )),
        concurrent(lambda s: daily_practice.skip(
            s, USER, practice_id, focus=[], locale="ko", timezone_name=SEOUL
        )),
        concurrent(lambda s: daily_practice.accept(
            s, USER, practice_id, locale="ko", timezone_name=SEOUL
        )),
    )
    held = [row for row in tables[DailyPracticeOffer] if row.status in {"accepted", "completed"}]
    assert len(held) == 1


async def test_concurrent_routine_requests_create_one_routine(concurrent, pool, tables, clock):
    first = await concurrent(lambda s: daily_practice.offer(
        s, USER, focus=[], locale="ko", timezone_name=SEOUL
    ))
    practice_id = str(first["practice"]["id"])
    await concurrent(lambda s: daily_practice.accept(s, USER, practice_id, locale="ko", timezone_name=SEOUL))
    await concurrent(lambda s: daily_practice.complete(s, USER, practice_id, locale="ko", timezone_name=SEOUL))
    results = await asyncio.gather(*[
        concurrent(lambda s: daily_practice.add_routine(
            s, USER, practice_id, locale="ko", timezone_name=SEOUL
        ))
        for _ in range(5)
    ])
    assert len(tables[Routine]) == 1
    assert len({result["routine"]["id"] for result in results}) == 1
