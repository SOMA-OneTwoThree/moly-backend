"""API 전이·오류·시간대·멱등 — 실제 라우터와 서비스를 메모리 세션 위에서 돌린다.

실제 Postgres의 제약·잠금은 tests/integration/test_daily_practice_local.py가 검증한다.
"""
import asyncio
import random
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.errors import AppError
from app.core.security import get_current_user
from app.main import app
from app.models.daily_practice import DailyPracticeOffer, DailyPracticeTemplate
from app.models.routine import Routine, RoutineCompletion, RoutineSchedule, RoutineSkip
from app.services import daily_practice, llm, privacy
from tests.fake_db import FakeDbSession

USER = "00000000-0000-0000-0000-000000000001"
OTHER = uuid.UUID("00000000-0000-0000-0000-000000000002")
SEOUL = "Asia/Seoul"
LA = "America/Los_Angeles"
NOON_SEOUL = datetime(2026, 11, 1, 12, 0, tzinfo=ZoneInfo(SEOUL)).astimezone(timezone.utc)
HEADERS = {"X-App-Timezone": SEOUL, "X-App-Locale": "ko"}
TODAY_PATH = "/daily-practice"
TABLES = (DailyPracticeTemplate, DailyPracticeOffer, Routine, RoutineSchedule, RoutineCompletion, RoutineSkip)
VERBS = ("accept", "complete", "uncomplete", "routine")


def template(n, is_active=True):
    return SimpleNamespace(
        id=uuid.UUID(int=n), is_active=is_active,
        title_ko=f"물 {n}잔 마시기", title_en=f"Drink water {n}", title_ja=f"お水{n}杯飲む",
    )


class Session(FakeDbSession):
    """FakeDbSession에 쓰기를 더한 세션. 실제 DB의 두 유일 제약을 flush에서 검사한다."""

    def __init__(self, tables):
        self.tables = tables
        self.statements = []
        self.rolled_back = False

    def add(self, row):
        self.tables[type(row)].append(row)

    async def get(self, model, key):
        return next((row for row in self.tables[model] if row.id == key), None)

    async def flush(self):
        rows = self.tables[DailyPracticeOffer]
        chosen = [(r.user_id, r.local_date) for r in rows if r.status != "offered"]
        assert len(chosen) == len(set(chosen)), "daily_practice_offers_one_choice_uq 위반"
        shown = [(r.user_id, r.template_id, r.local_date) for r in rows]
        assert len(shown) == len(set(shown)), "(user_id, template_id, local_date) 유일 위반"

    async def commit(self):
        await self.flush()


@pytest.fixture
def clock():
    return SimpleNamespace(now=NOON_SEOUL)


@pytest.fixture
def tables():
    return {model: [] for model in TABLES}


@pytest.fixture
def pool(tables):
    tables[DailyPracticeTemplate].extend(template(i) for i in range(1, 11))
    return tables[DailyPracticeTemplate]


@pytest.fixture
def client(monkeypatch, tables, clock):
    session = Session(tables)

    async def session_dep():
        yield session

    async def profile(_session, _user_id):
        return SimpleNamespace(timezone=SEOUL)

    async def no_lock(_session, _uid):
        return None

    async def no_llm(*_args, **_kwargs):
        raise AssertionError("LLM을 부르지 않는다")

    monkeypatch.setattr(daily_practice, "_load_profile", profile)
    monkeypatch.setattr(daily_practice, "advisory_xact_lock", no_lock)
    monkeypatch.setattr(daily_practice, "_clock", lambda _now=None: clock.now)
    monkeypatch.setattr(llm, "generate", no_llm)
    app.dependency_overrides[get_session] = session_dep
    app.dependency_overrides[get_current_user] = lambda: USER
    yield TestClient(app)
    app.dependency_overrides.clear()


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def code(response):
    return response.status_code, response.json()["error"]["code"]


def get(client, headers=HEADERS):
    return ok(client.get(TODAY_PATH, headers=headers))


def offer(client, headers=HEADERS):
    return ok(client.post(f"{TODAY_PATH}/offer", headers=headers))


def act(client, verb, practice_id, headers=HEADERS):
    return client.post(f"{TODAY_PATH}/{practice_id}/{verb}", headers=headers)


def accepted(client):
    practice_id = offer(client)["options"][0]["id"]
    ok(act(client, "accept", practice_id))
    return practice_id


def completed(client):
    practice_id = accepted(client)
    ok(act(client, "complete", practice_id))
    return practice_id


def next_midnight(clock):
    local = clock.now.astimezone(ZoneInfo(SEOUL)).date() + timedelta(days=1)
    return datetime(local.year, local.month, local.day, tzinfo=ZoneInfo(SEOUL)).astimezone(timezone.utc)


def history(tables, template_id, local_date, *, status="offered", user_id=None):
    tables[DailyPracticeOffer].append(DailyPracticeOffer(
        id=uuid.uuid4(), user_id=user_id or uuid.UUID(USER), template_id=template_id,
        local_date=local_date, status=status, options_expire_at=NOON_SEOUL - timedelta(days=100),
        deadline=None if status == "offered" else NOON_SEOUL - timedelta(days=100),
    ))


def titles(body):
    return {option["title"] for option in body["options"]}


# ── 선택 함수 ────────────────────────────────────────────────────

IDS = [uuid.UUID(int=i) for i in range(1, 7)]


def test_pick_is_three_distinct_and_follows_the_random_source():
    first = daily_practice.pick_options(IDS, {}, random.Random(1))
    assert len(first) == len(set(first)) == 3
    assert first == daily_practice.pick_options(IDS, {}, random.Random(1))
    assert {tuple(daily_practice.pick_options(IDS, {}, random.Random(s))) for s in range(20)} != {tuple(first)}


def test_pick_avoids_recent_when_enough_are_fresh():
    recent = {IDS[0]: date(2026, 10, 31), IDS[1]: date(2026, 10, 30), IDS[2]: date(2026, 10, 29)}
    for seed in range(20):
        assert set(daily_practice.pick_options(IDS, recent, random.Random(seed))) == set(IDS[3:])


def test_pick_fills_with_the_least_recently_shown():
    recent = {
        IDS[0]: date(2026, 10, 31), IDS[1]: date(2026, 10, 1), IDS[2]: date(2026, 10, 15),
        IDS[3]: date(2026, 10, 20), IDS[4]: date(2026, 10, 25),
    }
    for seed in range(20):
        picked = daily_practice.pick_options(IDS, recent, random.Random(seed))
        assert IDS[5] in picked and set(picked) == {IDS[5], IDS[1], IDS[2]}


@pytest.mark.parametrize("count", [0, 1, 2])
def test_pick_returns_what_exists_when_there_are_fewer_than_three(count):
    assert len(daily_practice.pick_options(IDS[:count], {}, random.Random(1))) == count


# ── 조회와 제안 ──────────────────────────────────────────────────

def test_get_never_creates_options(client, pool, tables):
    response = client.get(TODAY_PATH, headers=HEADERS)
    assert response.json() == {
        "local_date": "2026-11-01",
        "expires_at": "2026-11-01T15:00:00+00:00",
        "locale": "ko",
        "can_choose": True,
        "options": [],
        "practice": None,
    }
    assert response.headers["cache-control"] == "private, no-store"
    assert tables[DailyPracticeOffer] == []


def test_get_returns_existing_options(client, pool):
    made = offer(client)
    assert get(client) == made


def test_offer_creates_three_options_with_the_contract_shape(client, pool, tables):
    body = offer(client)
    assert body["can_choose"] is True and body["practice"] is None and len(body["options"]) == 3
    for option in body["options"]:
        assert set(option) == {"id", "status", "title", "routine_added", "deadline"}
        assert option["status"] == "offered" and option["routine_added"] is False
        assert option["deadline"] is None
    rows = tables[DailyPracticeOffer]
    assert len(rows) == 3
    assert {r.local_date for r in rows} == {date(2026, 11, 1)}
    assert {r.options_expire_at for r in rows} == {datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc)}


def test_offer_repeats_the_same_three_in_the_same_order_until_midnight(client, pool, tables, clock):
    first = offer(client)
    clock.now = next_midnight(clock) - timedelta(seconds=1)
    assert offer(client)["options"] == first["options"]
    assert len(tables[DailyPracticeOffer]) == 3
    assert get(client)["options"] == first["options"]


def test_new_local_day_gets_new_options_and_keeps_the_old_as_history(client, pool, tables, clock):
    first = offer(client)
    clock.now = next_midnight(clock)
    assert get(client)["options"] == []
    second = offer(client)
    assert second["local_date"] == "2026-11-02"
    assert titles(first).isdisjoint(titles(second))
    assert len(tables[DailyPracticeOffer]) == 6


@pytest.mark.parametrize(("count", "expected"), [(0, 0), (1, 1), (2, 2), (3, 3), (5, 3)])
def test_option_count_follows_the_active_pool(client, tables, count, expected):
    tables[DailyPracticeTemplate].extend(template(i + 1) for i in range(count))
    body = offer(client)
    assert len(body["options"]) == expected and body["can_choose"] is True


def test_inactive_templates_are_not_offered_but_stay_on_rows_already_shown(client, tables):
    tables[DailyPracticeTemplate].extend([template(1), template(2, is_active=False)])
    assert titles(offer(client)) == {"물 1잔 마시기"}
    tables[DailyPracticeTemplate][0].is_active = False
    assert titles(get(client)) == {"물 1잔 마시기"}


def test_templates_shown_within_30_days_are_avoided_and_older_ones_are_not(client, tables):
    tables[DailyPracticeTemplate].extend(template(i) for i in range(1, 6))
    history(tables, uuid.UUID(int=1), date(2026, 10, 31))
    history(tables, uuid.UUID(int=2), date(2026, 10, 2), status="completed")
    history(tables, uuid.UUID(int=3), date(2026, 10, 1))
    assert titles(offer(client)) == {"물 3잔 마시기", "물 4잔 마시기", "물 5잔 마시기"}


def test_fill_uses_the_least_recently_shown(client, tables):
    tables[DailyPracticeTemplate].extend(template(i) for i in range(1, 5))
    history(tables, uuid.UUID(int=1), date(2026, 10, 31))
    history(tables, uuid.UUID(int=2), date(2026, 10, 10))
    history(tables, uuid.UUID(int=3), date(2026, 10, 20))
    assert titles(offer(client)) == {"물 4잔 마시기", "물 2잔 마시기", "물 3잔 마시기"}


def test_another_users_history_does_not_affect_selection(client, tables):
    tables[DailyPracticeTemplate].extend(template(i) for i in range(1, 4))
    for n in (1, 2, 3):
        history(tables, uuid.UUID(int=n), date(2026, 11, 1), user_id=OTHER)
    assert len(offer(client)["options"]) == 3


@pytest.mark.parametrize(("header", "locale", "title"), [
    ("ko-KR", "ko", "물 1잔 마시기"), ("en", "en", "Drink water 1"), ("ja-JP", "ja", "お水1杯飲む"),
    (None, "en", "Drink water 1"), ("zh-Hant-TW", "en", "Drink water 1"),
])
def test_titles_follow_the_requested_language(client, tables, header, locale, title):
    tables[DailyPracticeTemplate].append(template(1))
    headers = {"X-App-Timezone": SEOUL} | ({"X-App-Locale": header} if header else {})
    body = offer(client, headers)
    assert body["locale"] == locale and body["options"][0]["title"] == title


# ── 선택 ────────────────────────────────────────────────────────

def test_accept_sets_the_24h_deadline_and_hides_the_options(client, pool, clock, tables):
    options = offer(client)["options"]
    body = ok(act(client, "accept", options[0]["id"]))
    deadline = clock.now + timedelta(hours=24)
    assert body["practice"]["id"] == options[0]["id"] and body["practice"]["status"] == "accepted"
    assert datetime.fromisoformat(body["practice"]["deadline"]) == deadline
    assert datetime.fromisoformat(body["expires_at"]) == deadline
    assert body["can_choose"] is False and body["options"] == []
    assert sorted(r.status for r in tables[DailyPracticeOffer]) == ["accepted", "offered", "offered"]


def test_accept_is_idempotent_and_keeps_the_first_deadline(client, pool, clock):
    practice_id = offer(client)["options"][0]["id"]
    first = ok(act(client, "accept", practice_id))
    clock.now += timedelta(minutes=5)
    assert ok(act(client, "accept", practice_id)) == first


def test_second_option_cannot_be_accepted_while_one_is_live(client, pool):
    options = offer(client)["options"]
    ok(act(client, "accept", options[0]["id"]))
    assert code(act(client, "accept", options[1]["id"])) == (409, "DAILY_PRACTICE_INVALID_STATE")


def test_second_option_cannot_be_accepted_after_completing_today(client, pool):
    options = offer(client)["options"]
    ok(act(client, "accept", options[0]["id"]))
    ok(act(client, "complete", options[0]["id"]))
    assert code(act(client, "accept", options[1]["id"])) == (409, "DAILY_PRACTICE_INVALID_STATE")


def test_accepting_after_completion_is_refused(client, pool):
    assert code(act(client, "accept", completed(client))) == (409, "DAILY_PRACTICE_INVALID_STATE")


def test_yesterdays_option_cannot_be_accepted(client, pool, clock):
    practice_id = offer(client)["options"][0]["id"]
    clock.now = next_midnight(clock)
    assert code(act(client, "accept", practice_id)) == (409, "DAILY_PRACTICE_INVALID_STATE")


def test_accepted_row_past_its_deadline_is_not_idempotent(client, pool, clock):
    practice_id = accepted(client)
    clock.now += timedelta(hours=24)
    assert code(act(client, "accept", practice_id)) == (409, "DAILY_PRACTICE_INVALID_STATE")


def test_offer_creates_nothing_while_a_practice_is_live_or_after_choosing(client, pool, tables, clock):
    practice_id = accepted(client)
    assert offer(client)["options"] == []
    ok(act(client, "complete", practice_id))
    assert offer(client)["options"] == []
    assert len(tables[DailyPracticeOffer]) == 3


# ── 완료와 취소 ──────────────────────────────────────────────────

def test_complete_records_the_local_date_and_is_idempotent(client, pool, tables):
    practice_id = accepted(client)
    body = ok(act(client, "complete", practice_id))
    assert body["practice"]["status"] == "completed"
    row = next(r for r in tables[DailyPracticeOffer] if r.status == "completed")
    assert row.completed_local_date == date(2026, 11, 1)
    assert ok(act(client, "complete", practice_id)) == body


def test_complete_is_refused_from_offered(client, pool):
    practice_id = offer(client)["options"][0]["id"]
    assert code(act(client, "complete", practice_id)) == (409, "DAILY_PRACTICE_INVALID_STATE")


def test_complete_works_until_the_deadline_and_is_expired_from_it(client, pool, clock):
    practice_id = accepted(client)
    start = clock.now
    clock.now = start + timedelta(hours=24)
    assert code(act(client, "complete", practice_id)) == (409, "DAILY_PRACTICE_EXPIRED")
    clock.now = start + timedelta(hours=24) - timedelta(seconds=1)
    assert ok(act(client, "complete", practice_id))["practice"]["status"] == "completed"


def test_repeating_complete_after_the_deadline_stays_idempotent(client, pool, clock):
    practice_id = completed(client)
    clock.now += timedelta(hours=25)
    assert act(client, "complete", practice_id).status_code == 200


def test_uncomplete_returns_to_accepted_and_is_idempotent(client, pool, tables):
    practice_id = completed(client)
    body = ok(act(client, "uncomplete", practice_id))
    assert body["practice"]["status"] == "accepted"
    assert body["can_choose"] is False
    assert all(r.completed_local_date is None for r in tables[DailyPracticeOffer])
    assert ok(act(client, "uncomplete", practice_id)) == body
    assert ok(act(client, "complete", practice_id))["practice"]["status"] == "completed"


def test_uncomplete_is_refused_from_offered(client, pool):
    practice_id = offer(client)["options"][0]["id"]
    assert code(act(client, "uncomplete", practice_id)) == (409, "DAILY_PRACTICE_INVALID_STATE")


def test_uncomplete_is_refused_once_a_routine_was_added(client, pool):
    practice_id = completed(client)
    ok(act(client, "routine", practice_id))
    assert code(act(client, "uncomplete", practice_id)) == (409, "DAILY_PRACTICE_INVALID_STATE")


def test_uncomplete_is_refused_once_another_practice_was_chosen(client, pool, clock):
    first = accepted(client)
    clock.now = next_midnight(clock) + timedelta(hours=1)
    ok(act(client, "complete", first))
    second = offer(client)["options"][0]["id"]
    ok(act(client, "accept", second))
    assert code(act(client, "uncomplete", first)) == (409, "DAILY_PRACTICE_INVALID_STATE")


@pytest.mark.parametrize("prepare", [completed, accepted])
def test_uncomplete_past_the_deadline_is_expired(client, pool, clock, prepare):
    practice_id = prepare(client)
    clock.now += timedelta(hours=24)
    assert code(act(client, "uncomplete", practice_id)) == (409, "DAILY_PRACTICE_EXPIRED")


# ── can_choose, practice, options ───────────────────────────────

def test_chosen_and_completed_today_blocks_until_the_next_local_day(client, pool, clock):
    completed(client)
    body = get(client)
    assert body["can_choose"] is False and body["options"] == []
    assert body["practice"]["status"] == "completed"
    assert body["expires_at"] == "2026-11-01T15:00:00+00:00"
    clock.now = next_midnight(clock)
    body = get(client)
    assert body["can_choose"] is True and body["practice"] is None


def test_live_practice_blocks_choosing_past_midnight_until_the_deadline(client, pool, clock):
    practice_id = accepted(client)
    start = clock.now
    clock.now = next_midnight(clock)
    body = offer(client)
    assert body["can_choose"] is False and body["options"] == []
    assert body["practice"]["id"] == practice_id and body["local_date"] == "2026-11-02"
    assert datetime.fromisoformat(body["expires_at"]) == start + timedelta(hours=24)


def test_expired_practice_silently_stops_being_current(client, pool, clock):
    accepted(client)
    clock.now += timedelta(hours=24)
    body = get(client)
    assert body["practice"] is None and body["can_choose"] is True
    assert body["local_date"] == "2026-11-02"
    assert len(offer(client)["options"]) == 3


def test_yesterdays_pick_completed_today_leaves_today_free(client, pool, clock):
    practice_id = accepted(client)
    clock.now = next_midnight(clock) + timedelta(hours=1)
    body = ok(act(client, "complete", practice_id))
    assert body["practice"]["id"] == practice_id and body["practice"]["status"] == "completed"
    assert body["can_choose"] is True and body["options"] == []

    body = offer(client)
    assert body["practice"]["id"] == practice_id and len(body["options"]) == 3
    assert body["expires_at"] == "2026-11-02T15:00:00+00:00"


def test_todays_pick_replaces_yesterdays_completed_row_as_the_practice(client, pool, clock):
    first = accepted(client)
    clock.now = next_midnight(clock) + timedelta(hours=1)
    ok(act(client, "complete", first))
    second = offer(client)["options"][0]["id"]
    body = ok(act(client, "accept", second))
    assert body["practice"]["id"] == second and body["can_choose"] is False
    body = ok(act(client, "complete", second))
    assert body["practice"]["id"] == second and body["can_choose"] is False


def test_yesterdays_pick_completed_yesterday_is_not_the_practice_today(client, pool, clock):
    completed(client)
    clock.now = next_midnight(clock)
    assert get(client)["practice"] is None


# ── 루틴 추가 ───────────────────────────────────────────────────

def test_routine_uses_the_requested_title_and_fixed_defaults(client, pool, tables):
    practice_id = completed(client)
    row = next(r for r in tables[DailyPracticeOffer] if r.status == "completed")
    source = next(t for t in pool if t.id == row.template_id)
    response = ok(act(client, "routine", practice_id, {**HEADERS, "X-App-Locale": "ja"}))
    routine = response["routine"]
    assert routine["name"] == source.title_ja
    assert routine["icon"] == "seedling" and routine["color"] == "peach"
    assert routine["days_of_week"] == [1, 2, 3, 4, 5, 6, 7]
    assert routine["reminder_enabled"] is False and routine["reminder_time"] is None
    assert response["today"]["practice"]["routine_added"] is True
    saved = tables[Routine][0]
    assert saved.name_i18n == {"ko": source.title_ko, "en": source.title_en, "ja": source.title_ja}
    assert saved.frequency_per_week == 7
    schedule = tables[RoutineSchedule][0]
    assert schedule.routine_id == saved.id and schedule.effective_from == date(2026, 11, 1)
    assert schedule.days_of_week == [1, 2, 3, 4, 5, 6, 7]


def test_routine_repeat_returns_the_same_routine(client, pool, tables):
    practice_id = completed(client)
    first = ok(act(client, "routine", practice_id))
    second = ok(act(client, "routine", practice_id))
    assert second == first
    assert len(tables[Routine]) == 1 and len(tables[RoutineSchedule]) == 1


def test_routine_repeat_reports_todays_routine_record(client, pool, tables):
    practice_id = completed(client)
    routine_id = ok(act(client, "routine", practice_id))["routine"]["id"]
    tables[RoutineCompletion].append(SimpleNamespace(
        user_id=uuid.UUID(USER), routine_id=uuid.UUID(routine_id), activity_date=date(2026, 11, 1)
    ))
    assert ok(act(client, "routine", practice_id))["routine"]["completed_today"] is True


def test_soft_deleted_routine_clears_routine_added_and_a_repeat_repoints(client, pool, tables, clock):
    practice_id = completed(client)
    first = ok(act(client, "routine", practice_id))
    tables[Routine][0].deleted_at = clock.now
    assert get(client)["practice"]["routine_added"] is False
    second = ok(act(client, "routine", practice_id))
    assert second["routine"]["id"] != first["routine"]["id"]
    assert second["today"]["practice"]["routine_added"] is True
    row = next(r for r in tables[DailyPracticeOffer] if r.status == "completed")
    assert str(row.routine_id) == second["routine"]["id"]
    assert len(tables[Routine]) == 2


def test_uncomplete_is_allowed_after_the_routine_was_soft_deleted(client, pool, tables, clock):
    practice_id = completed(client)
    ok(act(client, "routine", practice_id))
    tables[Routine][0].deleted_at = clock.now
    assert ok(act(client, "uncomplete", practice_id))["practice"]["status"] == "accepted"
    row = next(r for r in tables[DailyPracticeOffer] if r.status == "accepted")
    assert row.routine_id is None


def test_routine_can_still_be_added_after_the_deadline(client, pool, clock):
    practice_id = completed(client)
    clock.now += timedelta(hours=30)
    assert ok(act(client, "routine", practice_id))["routine"]["icon"] == "seedling"


@pytest.mark.parametrize("prepare", [lambda c: offer(c)["options"][0]["id"], accepted])
def test_routine_is_only_allowed_from_completed(client, pool, tables, prepare):
    assert code(act(client, "routine", prepare(client))) == (409, "DAILY_PRACTICE_INVALID_STATE")
    assert tables[Routine] == []


# ── 오류 코드 ───────────────────────────────────────────────────

@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("practice_id", [str(uuid.UUID(int=12345)), "not-a-uuid"])
def test_unknown_id_is_404(client, pool, verb, practice_id):
    assert code(act(client, verb, practice_id)) == (404, "DAILY_PRACTICE_NOT_FOUND")


@pytest.mark.parametrize("verb", VERBS)
def test_another_users_id_is_404(client, pool, tables, verb):
    history(tables, pool[0].id, date(2026, 11, 1), status="completed", user_id=OTHER)
    row = tables[DailyPracticeOffer][0]
    assert code(act(client, verb, row.id)) == (404, "DAILY_PRACTICE_NOT_FOUND")
    assert row.status == "completed"


def test_skip_endpoint_is_gone(client, pool):
    assert client.post(f"{TODAY_PATH}/{uuid.uuid4()}/skip", headers=HEADERS).status_code == 404


@pytest.mark.parametrize("verb", ["get", "offer", *VERBS])
def test_invalid_timezone_is_422_with_no_store(client, pool, verb):
    headers = {"X-App-Timezone": "Mars/Olympus"}
    if verb == "get":
        response = client.get(TODAY_PATH, headers=headers)
    elif verb == "offer":
        response = client.post(f"{TODAY_PATH}/offer", headers=headers)
    else:
        response = act(client, verb, uuid.uuid4(), headers)
    assert code(response) == (422, "APP_TIMEZONE_INVALID")
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("verb", ["get", "offer", *VERBS])
def test_account_being_deleted_is_409(client, monkeypatch, pool, tables, verb):
    async def deleting(_session, _uid):
        raise AppError("ACCOUNT_DELETING", 409, "계정 삭제를 처리하고 있어요.")

    monkeypatch.setattr(privacy, "ensure_subject_active", deleting)
    if verb == "get":
        response = client.get(TODAY_PATH, headers=HEADERS)
    elif verb == "offer":
        response = client.post(f"{TODAY_PATH}/offer", headers=HEADERS)
    else:
        response = act(client, verb, uuid.uuid4())
    assert code(response) == (409, "ACCOUNT_DELETING")
    assert response.headers["cache-control"] == "private, no-store"
    assert tables[DailyPracticeOffer] == []


@pytest.mark.parametrize("verb", ["offer", *VERBS])
def test_writes_take_the_lock_before_checking_the_deletion_barrier(client, monkeypatch, pool, verb):
    calls = []

    async def lock(_session, _uid):
        calls.append("lock")

    async def barrier(_session, _uid):
        calls.append("barrier")

    practice_id = offer(client)["options"][0]["id"]
    monkeypatch.setattr(daily_practice, "advisory_xact_lock", lock)
    monkeypatch.setattr(privacy, "ensure_subject_active", barrier)
    if verb == "offer":
        offer(client)
    else:
        act(client, verb, practice_id)
    assert calls == ["lock", "barrier"]


def test_oversized_locale_header_is_422(client, pool):
    response = client.get(TODAY_PATH, headers={**HEADERS, "X-App-Locale": "x" * 65})
    assert code(response) == (422, "VALIDATION")


@pytest.mark.parametrize(("method", "path"), [
    ("get", TODAY_PATH),
    ("post", f"{TODAY_PATH}/offer"),
    *[("post", f"{TODAY_PATH}/{uuid.UUID(int=1)}/{verb}") for verb in VERBS],
])
def test_every_endpoint_requires_a_bearer_token(method, path):
    response = getattr(TestClient(app), method)(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.parametrize("verb", VERBS)
def test_every_write_response_is_no_store(client, pool, verb):
    response = act(client, verb, offer(client)["options"][0]["id"])
    assert response.headers["cache-control"] == "private, no-store"


# ── 현지 자정과 시간대 ──────────────────────────────────────────

def test_local_date_follows_the_request_timezone(client, pool, clock, tables):
    clock.now = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)
    body = offer(client, {"X-App-Timezone": LA, "X-App-Locale": "en"})
    assert body["local_date"] == "2026-10-31"
    assert body["expires_at"] == "2026-11-01T07:00:00+00:00"
    assert tables[DailyPracticeOffer][0].options_expire_at == datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)


def test_expires_at_handles_a_daylight_saving_day(client, pool, clock):
    clock.now = datetime(2026, 11, 1, 12, 0, tzinfo=ZoneInfo(LA)).astimezone(timezone.utc)
    body = offer(client, {"X-App-Timezone": LA})
    assert body["local_date"] == "2026-11-01"
    assert body["expires_at"] == "2026-11-02T08:00:00+00:00"


def test_deadline_is_24_elapsed_hours_across_a_daylight_saving_change(client, pool, clock):
    headers = {"X-App-Timezone": LA}
    clock.now = datetime(2026, 10, 31, 23, 30, tzinfo=ZoneInfo(LA)).astimezone(timezone.utc)
    practice_id = offer(client, headers)["options"][0]["id"]
    body = ok(act(client, "accept", practice_id, headers))
    assert datetime.fromisoformat(body["practice"]["deadline"]) == clock.now + timedelta(hours=24)


def test_missing_timezone_header_uses_the_stored_timezone(client, pool):
    body = offer(client, {"X-App-Locale": "ko"})
    assert body["local_date"] == "2026-11-01"
    assert body["expires_at"] == "2026-11-01T15:00:00+00:00"


def test_moving_east_past_midnight_starts_a_new_local_day(client, pool, clock):
    clock.now = datetime(2026, 11, 1, 23, 0, tzinfo=ZoneInfo(LA)).astimezone(timezone.utc)
    west = offer(client, {"X-App-Timezone": LA})
    east = get(client)
    assert east["local_date"] == "2026-11-02" and east["options"] == []
    assert code(act(client, "accept", west["options"][0]["id"])) == (409, "DAILY_PRACTICE_INVALID_STATE")


def test_moving_west_onto_the_same_local_date_refills_without_repeating_rows(client, tables, clock):
    tables[DailyPracticeTemplate].extend(template(i) for i in range(1, 7))
    first = offer(client)
    clock.now = datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc)
    second = offer(client, {"X-App-Timezone": LA, "X-App-Locale": "ko"})
    assert second["local_date"] == "2026-11-01"
    assert len(second["options"]) == 3 and titles(first).isdisjoint(titles(second))


def test_moving_west_cannot_choose_twice_on_one_local_date(client, pool, tables, clock):
    practice_id = accepted(client)
    ok(act(client, "complete", practice_id))
    clock.now = datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc)
    body = offer(client, {"X-App-Timezone": LA, "X-App-Locale": "ko"})
    assert body["local_date"] == "2026-11-01"
    assert body["can_choose"] is False and body["options"] == []
    assert len(tables[DailyPracticeOffer]) == 3


# ── 동시성: 잠금을 잡은 뒤 다시 확인하는지 ─────────────────────

class _LockingSession(Session):
    """advisory lock을 사용자별 asyncio.Lock으로 흉내낸다. commit까지 유지한다."""

    held = None

    async def commit(self):
        await super().commit()
        self.release()

    def release(self):
        if self.held is not None:
            self.held.release()
            self.held = None


@pytest.fixture
def concurrent(monkeypatch, tables, clock):
    locks: dict = {}

    async def lock(session, uid):
        held = locks.setdefault(uid, asyncio.Lock())
        await held.acquire()
        session.held = held
        await asyncio.sleep(0)

    monkeypatch.setattr(daily_practice, "advisory_xact_lock", lock)
    monkeypatch.setattr(daily_practice, "_clock", lambda _now=None: clock.now)

    async def run(call):
        session = _LockingSession(tables)
        try:
            return await call(session)
        except AppError as error:
            return error
        finally:
            session.release()

    return run


KW = {"locale": "ko", "timezone_name": SEOUL}


async def test_concurrent_offers_create_one_set(concurrent, pool, tables):
    results = await asyncio.gather(*[
        concurrent(lambda s, i=i: daily_practice.offer(s, USER, rng=random.Random(i), **KW))
        for i in range(8)
    ])
    assert len(tables[DailyPracticeOffer]) == 3
    assert len({tuple(o["id"] for o in result["options"]) for result in results}) == 1


async def test_concurrent_accepts_choose_exactly_one(concurrent, pool, tables):
    made = await concurrent(lambda s: daily_practice.offer(s, USER, **KW))
    ids = [str(o["id"]) for o in made["options"]]
    results = await asyncio.gather(*[
        concurrent(lambda s, i=i: daily_practice.accept(s, USER, i, **KW)) for i in ids
    ])
    assert sum(isinstance(r, dict) for r in results) == 1
    assert {r.code for r in results if isinstance(r, AppError)} == {"DAILY_PRACTICE_INVALID_STATE"}
    assert sorted(r.status for r in tables[DailyPracticeOffer]) == ["accepted", "offered", "offered"]


async def test_concurrent_routine_requests_create_one_routine(concurrent, pool, tables):
    made = await concurrent(lambda s: daily_practice.offer(s, USER, **KW))
    practice_id = str(made["options"][0]["id"])
    await concurrent(lambda s: daily_practice.accept(s, USER, practice_id, **KW))
    await concurrent(lambda s: daily_practice.complete(s, USER, practice_id, **KW))
    results = await asyncio.gather(*[
        concurrent(lambda s: daily_practice.add_routine(s, USER, practice_id, **KW)) for _ in range(5)
    ])
    assert len(tables[Routine]) == 1
    assert len({result["routine"]["id"] for result in results}) == 1
