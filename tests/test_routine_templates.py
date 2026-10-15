"""루틴 템플릿 목록·선택과 오늘 스킵 — 서비스 규칙(DB 없음)."""
from datetime import date, datetime, timezone
from types import SimpleNamespace
import uuid

import pytest

from app.core.app_day import AppDay
from app.core.errors import AppError
from app.models.routine import RoutineSchedule
from app.schemas.routine import RoutineTemplateSelectionRequest
from app.services import routine

UID = uuid.UUID("11111111-1111-1111-1111-111111111111")
DAY = AppDay.at(datetime(2026, 10, 8, 3, tzinfo=timezone.utc), "Asia/Seoul")
MORNING = SimpleNamespace(id="morning", name_i18n={"ko": "아침", "en": "Morning", "ja": "朝"})
HOME = SimpleNamespace(id="home", name_i18n={"ko": "생활", "en": "Home", "ja": "暮らし"})


def _template(template_id, ko, *, ja=None, days=(1, 2, 3, 4, 5, 6, 7), recommended=False):
    names = {"ko": ko, "en": ko.upper()} | ({"ja": ja} if ja else {})
    return SimpleNamespace(
        id=template_id, name_i18n=names, icon=f"{template_id}_icon", color="mint",
        days_of_week=list(days), is_recommended=recommended,
    )


CATALOGUE = [
    (MORNING, _template("make_bed", "이불 정리하기", ja="布団を整える", recommended=True)),
    (MORNING, _template("drink_water", "물 마시기", recommended=True)),
    (HOME, _template("do_laundry", "빨래하기", ja="洗濯する", days=(7, 7))),
]


class FakeSession:
    def __init__(self):
        self.added = []
        self.committed = False

    def add_all(self, rows):
        self.added.extend(rows)

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True


@pytest.fixture
def profile(monkeypatch):
    current = SimpleNamespace(id=UID, language="ja", timezone="Asia/Seoul", routine_template_selection_at=None)
    added: set[str] = set()
    locks = []

    async def _load_profile(session, user_id):
        return current

    async def _active_templates(session):
        return CATALOGUE

    async def _added_template_ids(session, uid):
        return set(added)

    async def _lock(session, uid):
        locks.append(uid)

    monkeypatch.setattr(routine, "_load_profile", _load_profile)
    monkeypatch.setattr(routine, "_active_templates", _active_templates)
    monkeypatch.setattr(routine, "_added_template_ids", _added_template_ids)
    monkeypatch.setattr(routine, "advisory_xact_lock", _lock)
    current.added = added
    current.locks = locks
    return current


async def test_templates_are_grouped_localized_and_marked(profile):
    profile.added.add("make_bed")
    out = await routine.routine_templates(FakeSession(), str(UID))
    assert out["selection_completed"] is False
    assert [c["id"] for c in out["categories"]] == ["morning", "home"]
    assert out["categories"][0]["name"] == "朝"
    make_bed, drink_water = out["categories"][0]["templates"]
    assert (make_bed["name"], make_bed["recommended"], make_bed["added"]) == ("布団を整える", True, True)
    assert (drink_water["name"], drink_water["added"]) == ("물 마시기", False)
    assert out["categories"][1]["templates"][0]["days_of_week"] == [7]


async def test_selection_creates_in_catalogue_order_and_skips_added_ones(profile):
    profile.added.add("make_bed")
    session = FakeSession()
    req = RoutineTemplateSelectionRequest(template_ids=["do_laundry", "make_bed", "drink_water"])
    out = await routine.select_templates(session, str(UID), req, DAY)
    assert profile.locks == [UID]
    assert [r["template_id"] for r in out["data"]] == ["drink_water", "do_laundry"]
    assert [r["name"] for r in out["data"]] == ["물 마시기", "洗濯する"]
    assert all(not r["completed_today"] and not r["skipped_today"] for r in out["data"])
    routines = [row for row in session.added if not isinstance(row, RoutineSchedule)]
    schedules = [row for row in session.added if isinstance(row, RoutineSchedule)]
    assert [r.created_at for r in routines] == sorted({r.created_at for r in routines})
    laundry = routines[1]
    assert (laundry.days_of_week, laundry.frequency_per_week, laundry.reminder_enabled) == ([7], 1, False)
    assert laundry.name_i18n == {"ko": "빨래하기", "en": "빨래하기".upper(), "ja": "洗濯する"}
    assert [(s.routine_id, s.effective_from, s.days_of_week) for s in schedules] == [
        (r.id, date(2026, 10, 8), r.days_of_week) for r in routines
    ]
    assert profile.routine_template_selection_at is not None and session.committed


async def test_empty_selection_only_marks_the_screen_as_done(profile):
    session = FakeSession()
    out = await routine.select_templates(session, str(UID), RoutineTemplateSelectionRequest(template_ids=[]), DAY)
    assert out == {"data": []} and session.added == []
    assert profile.routine_template_selection_at is not None and session.committed


async def test_repeated_selection_keeps_the_first_mark_and_creates_nothing(profile):
    first = datetime(2026, 10, 1, tzinfo=timezone.utc)
    profile.routine_template_selection_at = first
    profile.added.update({"make_bed", "drink_water"})
    req = RoutineTemplateSelectionRequest(template_ids=["make_bed", "drink_water"])
    out = await routine.select_templates(FakeSession(), str(UID), req, DAY)
    assert out == {"data": []}
    assert profile.routine_template_selection_at == first


async def test_unknown_or_inactive_template_rejects_the_whole_selection(profile):
    session = FakeSession()
    req = RoutineTemplateSelectionRequest(template_ids=["make_bed", "retired_one"])
    with pytest.raises(AppError) as error:
        await routine.select_templates(session, str(UID), req, DAY)
    assert (error.value.code, error.value.http_status) == ("VALIDATION", 422)
    assert error.value.details == {"template_ids": ["retired_one"]}
    assert session.added == [] and not session.committed
    assert profile.routine_template_selection_at is None


def test_selection_request_rejects_duplicates_too_many_and_bad_ids():
    from pydantic import ValidationError

    for body in (
        {"template_ids": ["make_bed", "make_bed"]},
        {"template_ids": [f"t{i}" for i in range(31)]},
        {"template_ids": ["Make-Bed"]},
        {"template_ids": None},
        {},
        {"template_ids": [], "extra": True},
    ):
        with pytest.raises(ValidationError):
            RoutineTemplateSelectionRequest.model_validate(body)
    assert RoutineTemplateSelectionRequest(template_ids=[f"t{i}" for i in range(30)])


class _First:
    def __init__(self, row):
        self.row = row

    def first(self):
        return self.row


class SkipSession:
    def __init__(self, completed):
        self.completed = completed
        self.statements = []
        self.committed = False

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        return _First((1,) if self.completed and len(self.statements) == 1 else None)

    async def commit(self):
        self.committed = True


@pytest.fixture
def owned(monkeypatch):
    row = SimpleNamespace(id=uuid.uuid4(), user_id=UID)

    async def _load_owned(session, uid, routine_id):
        return row

    monkeypatch.setattr(routine, "_load_owned", _load_owned)
    return row


async def test_skip_records_today_idempotently(owned):
    session = SkipSession(completed=False)
    await routine.skip(session, str(UID), str(owned.id), DAY)
    assert "routine_completions" in session.statements[0]
    assert "INSERT INTO routine_skips" in session.statements[1]
    assert "ON CONFLICT (routine_id, activity_date) DO NOTHING" in session.statements[1]
    assert session.committed


async def test_skip_after_completing_today_conflicts(owned):
    session = SkipSession(completed=True)
    with pytest.raises(AppError) as error:
        await routine.skip(session, str(UID), str(owned.id), DAY)
    assert (error.value.code, error.value.http_status) == ("ROUTINE_ALREADY_COMPLETED", 409)
    assert len(session.statements) == 1 and not session.committed


async def test_unskip_and_complete_clear_today_skip(owned, monkeypatch):
    session = SkipSession(completed=False)
    await routine.unskip(session, str(UID), str(owned.id), DAY)
    assert len(session.statements) == 1 and session.statements[0].startswith("DELETE FROM routine_skips")
    assert "routine_skips.activity_date" in session.statements[0] and session.committed

    class CompleteSession(SkipSession):
        async def execute(self, stmt):
            self.statements.append(str(stmt))
            return SimpleNamespace(scalar=lambda: 1)

    completing = CompleteSession(completed=False)
    out = await routine.complete(completing, str(UID), str(owned.id), DAY)
    assert out == {"completed_today": True, "completed_count_today": 1}
    assert completing.statements[0].startswith("INSERT INTO routine_completions")
    assert completing.statements[1].startswith("DELETE FROM routine_skips")


async def test_uncomplete_also_clears_a_skip_left_by_a_concurrent_request(owned):
    session = SkipSession(completed=False)
    await routine.uncomplete(session, str(UID), str(owned.id), DAY)
    assert [s.split(" WHERE")[0] for s in session.statements] == [
        "DELETE FROM routine_completions", "DELETE FROM routine_skips",
    ]
    assert all("activity_date" in s for s in session.statements) and session.committed
