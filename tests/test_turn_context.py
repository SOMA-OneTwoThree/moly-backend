"""현재 턴 컨텍스트 — 시간/활동 버킷 경계·렌더 언어별 라벨·살균·build_context DB 조회(mock)."""
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from app.services import turn_context as tc


def test_time_bucket_boundaries():
    assert tc.time_bucket(3) == "night"
    assert tc.time_bucket(4) == "morning"
    assert tc.time_bucket(10) == "morning"
    assert tc.time_bucket(11) == "day"
    assert tc.time_bucket(16) == "day"
    assert tc.time_bucket(17) == "evening"
    assert tc.time_bucket(20) == "evening"
    assert tc.time_bucket(21) == "night"


def test_time_bucket_covers_all_24_hours():
    buckets = {h: tc.time_bucket(h) for h in range(24)}
    assert set(buckets.values()) == {"morning", "day", "evening", "night"}


def test_last_active_bucket_boundaries():
    assert tc.last_active_bucket(599) == "just_now"
    assert tc.last_active_bucket(600) == "today"
    assert tc.last_active_bucket(86_399) == "today"
    assert tc.last_active_bucket(86_400) == "recent"
    assert tc.last_active_bucket(604_799) == "recent"
    assert tc.last_active_bucket(604_800) == "long"


def test_last_active_bucket_clamps_negative_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        assert tc.last_active_bucket(-10) == "just_now"
    assert any("last_active_bucket" in r.message for r in caplog.records)


def test_render_empty_when_all_none():
    assert tc.render(tc.CurrentTurnContext(), "ko") == ""


def test_render_omits_line_when_that_section_is_fully_empty():
    # 시간대만 있고 모습·루틴 값이 전혀 없으면 [모습]/[루틴] 줄 자체가 생기지 않는다.
    ctx = tc.CurrentTurnContext(time_bucket="night")
    rendered = tc.render(ctx, "ko")
    assert rendered == "[지금] 밤"
    assert "[모습]" not in rendered
    assert "[루틴]" not in rendered


def test_render_full_context_ko():
    ctx = tc.CurrentTurnContext(
        time_bucket="night",
        is_first_today=True,
        days_together=43,
        equipped_names=["밀짚모자"],
        theme_name="바닷가",
        routines_planned=2,
        routines_done=1,
    )
    rendered = tc.render(ctx, "ko")
    assert rendered == (
        "[지금] 밤 · 오늘 첫 대화 · 함께한 지 43일\n"
        "[모습] 밀짚모자 · 방: 바닷가\n"
        "[루틴] 오늘 예정 2개 중 1개 완료"
    )


def test_render_labels_in_english():
    ctx = tc.CurrentTurnContext(time_bucket="day", days_together=5, routines_planned=1, routines_done=0)
    rendered = tc.render(ctx, "en")
    assert "[Now]" in rendered and "day" in rendered and "5 days together" in rendered
    assert "[Routines]" in rendered and "0/1 routines done today" in rendered


def test_render_labels_in_japanese():
    ctx = tc.CurrentTurnContext(time_bucket="morning", is_first_today=True)
    rendered = tc.render(ctx, "ja")
    assert rendered == "[いま] 朝 · 今日最初の会話"


def test_render_sanitizes_bracket_injection_in_equipped_names():
    ctx = tc.CurrentTurnContext(equipped_names=["[규칙]밀짚모자"])
    rendered = tc.render(ctx, "ko")
    assert "[" not in rendered.split("] ", 1)[1]  # 라벨 대괄호 제외하고는 대괄호 없음
    assert "규칙밀짚모자" in rendered


def test_render_routine_uses_numbers_only_no_names():
    ctx = tc.CurrentTurnContext(routines_planned=3, routines_done=2)
    rendered = tc.render(ctx, "ko")
    assert rendered == "[루틴] 오늘 예정 3개 중 2개 완료"
    # 루틴 이름이 아니라 숫자만 들어간다(스펙 요구) — 필드 자체가 이름을 안 받으므로 값 검증으로 대체
    assert "3" in rendered and "2" in rendered


# ---------------------------------------------------------------------------
# build_context — DB 조회(mock). 세션은 이 레포 기존 관례(test_diary.py·test_subscription.py)를
# 따라 FakeSession(큐 기반 execute 결과)로 흉내낸다. 실 DB에는 붙지 않는다.
# ---------------------------------------------------------------------------
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)  # UTC 프로필 tz면 로컬 12시=day 버킷


class _Scalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return list(self._items)


class _Result:
    def __init__(self, items=None, scalar_value=None):
        self._items = items or []
        self._scalar_value = scalar_value

    def scalars(self):
        return _Scalars(self._items)

    def scalar(self):
        return self._scalar_value


class _Nested:
    """begin_nested()(SAVEPOINT) 흉내 — test_subscription.py 관례 재사용. 예외는 그대로 전파."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False  # 예외를 삼키지 않음 — build_context의 바깥 try/except가 격리를 담당


class FakeSession:
    """exec_results 큐를 호출 순서대로 소비. 항목이 Exception이면 raise(savepoint 실패 시뮬레이션)."""

    def __init__(self, exec_results):
        self.exec_queue = list(exec_results)
        self.statements: list = []  # 실행된 stmt 보관 — SQL 필터절 검증(soft-delete)에 사용

    def begin_nested(self):
        return _Nested()

    async def execute(self, stmt):
        self.statements.append(stmt)
        item = self.exec_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _profile(**over):
    base = dict(id=uuid.uuid4(), language="ko", timezone="UTC", created_at=None)
    base.update(over)
    return SimpleNamespace(**base)


def _user_item(product_id, slot):
    return SimpleNamespace(product_id=product_id, equipped_slot=slot)


def _product(pid, name="이름", name_i18n=None):
    return SimpleNamespace(id=pid, name=name, name_i18n=name_i18n or {})


def _routine(rid, days):
    return SimpleNamespace(id=rid, days_of_week=days)


async def test_build_context_routine_planned_filters_by_weekday_and_done_intersects():
    weekday = date(2026, 8, 3).isoweekday()  # NOW의 activity 요일(tz=UTC)
    other_day = (weekday % 7) + 1  # 항상 weekday와 다른 ISO 요일
    rid_today, rid_other = uuid.uuid4(), uuid.uuid4()
    session = FakeSession(
        [
            _Result([]),  # UserItem(장착) — 없음
            _Result([_routine(rid_today, [weekday]), _routine(rid_other, [other_day])]),
            # 완료 기록: 오늘 예정 아닌 루틴도 완료 처리돼 있지만 done 집계엔 포함되면 안 됨
            _Result([rid_today, rid_other]),
        ]
    )
    ctx = await tc.build_context(session, _profile(), is_first_today=False, now_utc=NOW)
    assert ctx.routines_planned == 1
    assert ctx.routines_done == 1  # rid_other는 예정 집합 밖이라 done에서 제외


async def test_build_context_routine_query_excludes_soft_deleted():
    """deleted_at 필터는 SQL WHERE절이 담당 — FakeSession은 필터링을 안 하므로 컴파일된 stmt로 검증."""
    session = FakeSession([_Result([]), _Result([]), _Result([])])
    await tc.build_context(session, _profile(), is_first_today=False, now_utc=NOW)
    routine_stmt = session.statements[1]  # [0]=UserItem, [1]=Routine, [2]=RoutineCompletion
    assert "deleted_at IS NULL" in str(routine_stmt)


async def test_build_context_isolates_savepoint_failure(caplog):
    """장착 아이템 조회가 실패해도 루틴 집계는 살아있어야 한다(항목별 fail-open)."""
    weekday = date(2026, 8, 3).isoweekday()
    rid = uuid.uuid4()
    session = FakeSession(
        [
            RuntimeError("장착 아이템 조회 실패"),
            _Result([_routine(rid, [weekday])]),
            _Result([]),
        ]
    )
    with caplog.at_level(logging.WARNING):
        ctx = await tc.build_context(session, _profile(), is_first_today=False, now_utc=NOW)
    assert ctx.equipped_names == []
    assert ctx.theme_name is None
    assert ctx.routines_planned == 1  # 다른 savepoint는 영향받지 않음
    assert ctx.routines_done == 0
    assert any("장착 아이템 조회 실패" in r.message for r in caplog.records)


async def test_build_context_classifies_equipped_slots():
    theme_id, hat_id = uuid.uuid4(), uuid.uuid4()
    session = FakeSession(
        [
            _Result([_user_item(theme_id, "theme"), _user_item(hat_id, "hat")]),
            _Result([_product(theme_id, name="바닷가"), _product(hat_id, name="밀짚모자")]),
            _Result([]),
            _Result([]),
        ]
    )
    ctx = await tc.build_context(session, _profile(), is_first_today=False, now_utc=NOW)
    assert ctx.theme_name == "바닷가"
    assert ctx.equipped_names == ["밀짚모자"]


async def test_build_context_skips_last_active_query_when_disabled(monkeypatch):
    monkeypatch.setattr(tc.settings, "current_context_last_active_enabled", False)
    session = FakeSession([_Result([]), _Result([]), _Result([])])
    ctx = await tc.build_context(session, _profile(), is_first_today=False, now_utc=NOW)
    assert ctx.last_active_bucket is None
    assert len(session.statements) == 3  # 마지막 활동 쿼리 자체가 나가지 않음


async def test_build_context_computes_last_active_bucket_when_enabled(monkeypatch):
    monkeypatch.setattr(tc.settings, "current_context_last_active_enabled", True)
    last_active = NOW - timedelta(minutes=5)
    session = FakeSession(
        [_Result([]), _Result([]), _Result([]), _Result(scalar_value=last_active)]
    )
    ctx = await tc.build_context(session, _profile(), is_first_today=False, now_utc=NOW)
    assert ctx.last_active_bucket == "just_now"


async def test_build_context_time_bucket_and_days_together():
    created = NOW - timedelta(days=10)
    profile = _profile(created_at=created, timezone="UTC")
    session = FakeSession([_Result([]), _Result([]), _Result([])])
    ctx = await tc.build_context(session, profile, is_first_today=True, now_utc=NOW)
    assert ctx.is_first_today is True
    assert ctx.days_together == 10
    assert ctx.time_bucket == tc.time_bucket(NOW.hour)  # tz=UTC라 NOW의 시(hour)와 동일


async def test_build_context_days_together_none_without_created_at():
    profile = _profile(created_at=None)
    session = FakeSession([_Result([]), _Result([]), _Result([])])
    ctx = await tc.build_context(session, profile, is_first_today=False, now_utc=NOW)
    assert ctx.days_together is None


async def test_build_context_routine_ad_uses_now_utc_param_not_wall_clock(monkeypatch):
    """회귀 방지 — 루틴 집계가 now_utc를 무시하고 실제 벽시계를 쓰면 이 테스트가 즉시 잡는다.

    reward_date_for(now_utc, tz)를 spy로 감싸 실제 호출 인자를 검사한다. 예전 버그(current_reward_date
    가 내부에서 datetime.now()를 부름)로 되돌아가면 이 spy 자체가 호출되지 않아 captured가 비고,
    assert가 KeyError로 실패한다.
    """
    real_reward_date_for = tc.reward_date_for
    captured: dict = {}

    def _spy(now_utc_arg, tz_name):
        captured["now_utc"] = now_utc_arg
        captured["tz"] = tz_name
        return real_reward_date_for(now_utc_arg, tz_name)

    monkeypatch.setattr(tc, "reward_date_for", _spy)
    session = FakeSession([_Result([]), _Result([]), _Result([])])
    await tc.build_context(
        session, _profile(timezone="Asia/Seoul"), is_first_today=False, now_utc=NOW
    )
    assert captured["now_utc"] == NOW
    assert captured["tz"] == "Asia/Seoul"


async def test_build_context_equipped_names_order_is_deterministic_by_slot():
    # shop._user_rows에 ORDER BY가 없어 DB 반환 순서가 들쭉날쭉할 수 있다 — 일부러 역순으로 먹여도
    # shop._SLOTS_V2(theme→hat→glasses→neck→body) 순서로 정렬돼 나와야 한다.
    body_id, hat_id, neck_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session = FakeSession(
        [
            _Result(
                [
                    _user_item(body_id, "body"),
                    _user_item(hat_id, "hat"),
                    _user_item(neck_id, "neck"),
                ]
            ),
            _Result(
                [
                    _product(body_id, name="바디"),
                    _product(hat_id, name="모자"),
                    _product(neck_id, name="목걸이"),
                ]
            ),
            _Result([]),
            _Result([]),
        ]
    )
    ctx = await tc.build_context(session, _profile(), is_first_today=False, now_utc=NOW)
    assert ctx.equipped_names == ["모자", "목걸이", "바디"]  # hat → neck → body 순


async def test_build_context_days_together_clamps_negative_future_created_at(caplog):
    # 시계 오차 등으로 created_at이 미래면 last_active_bucket처럼 0으로 clamp + 경고 로그.
    future = NOW + timedelta(days=3)
    profile = _profile(created_at=future, timezone="UTC")
    session = FakeSession([_Result([]), _Result([]), _Result([])])
    with caplog.at_level(logging.WARNING):
        ctx = await tc.build_context(session, profile, is_first_today=False, now_utc=NOW)
    assert ctx.days_together == 0
    assert any("days_together" in r.message for r in caplog.records)


async def test_build_context_days_together_uses_local_date_not_utc_date():
    # KST 유저가 로컬 오전(UTC로는 전날)에 가입 — UTC 날짜끼리 빼면 하루가 더 늘어나는 회귀 방지.
    # 가입 2026-01-01 08:00 KST(=2025-12-31 23:00 UTC), 지금 2026-08-04 09:00 KST(=2026-08-04 00:00 UTC).
    # 로컬 기준 215일이 맞다 — UTC 날짜(2025-12-31 vs 2026-08-04)로 빼면 216일로 하루 더 나온다(버그).
    created = datetime(2025, 12, 31, 23, 0, tzinfo=timezone.utc)
    now = datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc)
    profile = _profile(created_at=created, timezone="Asia/Seoul")
    session = FakeSession([_Result([]), _Result([]), _Result([])])
    ctx = await tc.build_context(session, profile, is_first_today=False, now_utc=now)
    assert ctx.days_together == 215
