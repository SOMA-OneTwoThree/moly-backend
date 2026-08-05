"""워커 틱 × 저녁 푸시 개인화 — 분기·창·백스톱·rollout 게이트(계획 v4 §3)."""
from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace

from app.core.time_utils import activity_date_for
from app.services import push_personalization as pp
from worker import tick

UID = uuid.uuid4()


def _profile():
    return SimpleNamespace(
        id=UID, timezone="Asia/Seoul", language="ko", nickname="승민"
    )


class _FakeSession:
    def __init__(self, profile):
        self._p = profile

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, model, pid):
        return self._p

    async def rollback(self):
        pass

    async def commit(self):
        pass


def _fake_sessionmaker(profile):
    def get():
        def maker():
            return _FakeSession(profile)
        return maker
    return get


def _row(now, slot):
    return pp.PushRow(
        user_id=UID,
        anchor_date=activity_date_for(now, "Asia/Seoul") - timedelta(days=1),
        send_slot=slot,
        body="요즘 그 프로젝트 잘 돼가? 얘기하자.",
        language="ko",
        source_kind="diary",
        sent_count=0,
    )


def _ctx(now, slot, rollout="all"):
    return pp.TickContext(
        cfg=pp.PushConfig(rollout=rollout, sources=frozenset({"diary", "transcript"})),
        rows={UID: _row(now, slot)},
        tick_start=None,
    )


def _spies(monkeypatch, *, personalized_result=1, chatted=False):
    calls = {"personalized": 0, "default": 0}

    async def _personalized(session, p, row, now):
        calls["personalized"] += 1
        return personalized_result

    async def _default(session, p, now=None):
        calls["default"] += 1
        return 1

    async def _chatted(session, p, now):
        return chatted

    monkeypatch.setattr(tick.notify, "notify_evening_personalized", _personalized)
    monkeypatch.setattr(tick.notify, "notify_evening", _default)
    monkeypatch.setattr(tick.push_personalization, "chatted_today", _chatted)
    return calls


KST20 = datetime(2026, 8, 5, 11, 0, tzinfo=timezone.utc)   # KST 20:00
KST2045 = datetime(2026, 8, 5, 11, 45, tzinfo=timezone.utc)  # KST 20:45
KST1430 = datetime(2026, 8, 5, 5, 30, tzinfo=timezone.utc)  # KST 14:30
KST05 = datetime(2026, 8, 4, 20, 0, tzinfo=timezone.utc)    # KST 05:00


async def test_night_cohort_personalized_wins_in_evening_branch(monkeypatch):
    calls = _spies(monkeypatch, personalized_result=1)
    monkeypatch.setattr(tick, "get_sessionmaker", _fake_sessionmaker(_profile()))
    out = await tick._process_user(KST20, UID, {"_push": _ctx(KST20, time(20, 0))})
    assert calls == {"personalized": 1, "default": 0}
    assert out["evening_personalized"] == 1 and out["evening"] == 0


async def test_night_cohort_inline_fallback_to_default_same_tick(monkeypatch):
    """slot=20:00 개인화 실패 → 같은 틱에서 디폴트 — 백스톱 공백 없음(C-1 해소 검증)."""
    calls = _spies(monkeypatch, personalized_result=0)
    monkeypatch.setattr(tick, "get_sessionmaker", _fake_sessionmaker(_profile()))
    out = await tick._process_user(KST20, UID, {"_push": _ctx(KST20, time(20, 0))})
    assert calls == {"personalized": 1, "default": 1}
    assert out["evening"] == 1 and out["evening_personalized"] == 0


async def test_slot_window_sends_personalized_outside_evening_hour(monkeypatch):
    calls = _spies(monkeypatch)
    monkeypatch.setattr(tick, "get_sessionmaker", _fake_sessionmaker(_profile()))
    out = await tick._process_user(KST1430, UID, {"_push": _ctx(KST1430, time(14, 30))})
    assert calls == {"personalized": 1, "default": 0}
    assert out["evening_personalized"] == 1


async def test_slot_window_respects_chatted_today(monkeypatch):
    calls = _spies(monkeypatch, chatted=True)
    monkeypatch.setattr(tick, "get_sessionmaker", _fake_sessionmaker(_profile()))
    await tick._process_user(KST1430, UID, {"_push": _ctx(KST1430, time(14, 30))})
    assert calls == {"personalized": 0, "default": 0}


async def test_slot_window_not_yet_open_no_send(monkeypatch):
    calls = _spies(monkeypatch)
    monkeypatch.setattr(tick, "get_sessionmaker", _fake_sessionmaker(_profile()))
    # 슬롯 15:00, 현재 14:30 — 창 미개시
    await tick._process_user(KST1430, UID, {"_push": _ctx(KST1430, time(15, 0))})
    assert calls == {"personalized": 0, "default": 0}


async def test_expired_slot_gets_default_backstop_at_evening(monkeypatch):
    """slot 09:00 창을 전부 놓쳐도(4틱 실패·재부팅 등) 20시 디폴트가 무조건 받아준다(M10)."""
    calls = _spies(monkeypatch)
    monkeypatch.setattr(tick, "get_sessionmaker", _fake_sessionmaker(_profile()))
    out = await tick._process_user(KST20, UID, {"_push": _ctx(KST20, time(9, 0))})
    assert calls == {"personalized": 0, "default": 1}
    assert out["evening"] == 1


async def test_late_slot_still_personalized_inside_evening_hour(monkeypatch):
    """slot 19:45의 창(~20:45)은 20시대로 걸친다 — 20:00 틱은 개인화 재시도, 20:45 틱은
    만료 백스톱(디폴트)."""
    calls = _spies(monkeypatch)
    monkeypatch.setattr(tick, "get_sessionmaker", _fake_sessionmaker(_profile()))
    await tick._process_user(KST20, UID, {"_push": _ctx(KST20, time(19, 45))})
    assert calls == {"personalized": 1, "default": 0}
    calls2 = _spies(monkeypatch, personalized_result=0)
    await tick._process_user(KST2045, UID, {"_push": _ctx(KST2045, time(19, 45))})
    assert calls2 == {"personalized": 0, "default": 1}  # 창 만료 → 백스톱


async def test_rollout_off_is_default_path_only(monkeypatch):
    """rollout off(또는 컨텍스트 부재) = 기존 저녁 디폴트 경로 그대로 — 개인화 코드 미주행."""
    calls = _spies(monkeypatch)
    monkeypatch.setattr(tick, "get_sessionmaker", _fake_sessionmaker(_profile()))
    out = await tick._process_user(KST20, UID, {"_push": _ctx(KST20, time(20, 0), rollout="off")})
    assert calls == {"personalized": 0, "default": 1} and out["evening"] == 1
    calls2 = _spies(monkeypatch)
    out2 = await tick._process_user(KST20, UID, {})  # 컨텍스트 자체가 없어도 동일
    assert calls2 == {"personalized": 0, "default": 1} and out2["evening"] == 1


async def test_gen_hour_calls_generation_only_when_rollout_open(monkeypatch):
    gen_calls = []

    async def _gen(session, p, now, cfg, token_cfg):
        gen_calls.append(p.id)
        return "ok"

    _spies(monkeypatch)
    monkeypatch.setattr(tick, "get_sessionmaker", _fake_sessionmaker(_profile()))
    monkeypatch.setattr(tick.push_personalization, "generate_for_user", _gen)
    out = await tick._process_user(KST05, UID, {"_push": _ctx(KST05, time(14, 30))})
    assert gen_calls == [UID] and out["push_gen_ok"] == 1
    out2 = await tick._process_user(
        KST05, UID, {"_push": _ctx(KST05, time(14, 30), rollout="off")}
    )
    assert gen_calls == [UID] and out2["push_gen_ok"] == 0  # off = 생성 자체 미주행


async def test_gen_hour_allowlist_scopes_generation(monkeypatch):
    """allowlist 모드: 명단 밖 유저는 생성(LLM 비용)도 하지 않는다 — 카나리 비용 격리."""
    gen_calls = []

    async def _gen(session, p, now, cfg, token_cfg):
        gen_calls.append(p.id)
        return "ok"

    _spies(monkeypatch)
    monkeypatch.setattr(tick, "get_sessionmaker", _fake_sessionmaker(_profile()))
    monkeypatch.setattr(tick.push_personalization, "generate_for_user", _gen)
    ctx = pp.TickContext(
        cfg=pp.PushConfig(rollout="allowlist", allowlist=frozenset()), rows={}
    )
    await tick._process_user(KST05, UID, {"_push": ctx})
    assert gen_calls == []
    ctx2 = pp.TickContext(
        cfg=pp.PushConfig(rollout="allowlist", allowlist=frozenset({UID})), rows={}
    )
    out = await tick._process_user(KST05, UID, {"_push": ctx2})
    assert gen_calls == [UID] and out["push_gen_ok"] == 1


async def test_gen_budget_guard_defers(monkeypatch):
    gen_calls = []

    async def _gen(*a, **k):
        gen_calls.append(1)
        return "ok"

    _spies(monkeypatch)
    monkeypatch.setattr(tick, "get_sessionmaker", _fake_sessionmaker(_profile()))
    monkeypatch.setattr(tick.push_personalization, "generate_for_user", _gen)
    import time as time_mod

    ctx = pp.TickContext(
        cfg=pp.PushConfig(rollout="all"),
        rows={},
        tick_start=time_mod.monotonic() - (pp.GEN_BUDGET_S + 1),  # 예산 초과 상태
    )
    out = await tick._process_user(KST05, UID, {"_push": ctx})
    assert gen_calls == [] and out["push_gen_deferred"] == 1  # 다음 틱 멱등 승계
