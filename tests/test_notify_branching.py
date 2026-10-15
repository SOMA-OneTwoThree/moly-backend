"""저녁 푸시 분기 사다리 계약 — 우선순위·경계·신규·폴백.

사다리는 순수 함수(notify._category)라 mock 없이 고정한다. 날짜 규약은 전 분기
활동일(로컬 04시 경계) 통일 — 00~04시 대화자가 어느 분기에도 안 걸리던 v1 구멍의 방지책.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import gating, notify, push, push_copy


def _cat(**over):
    base = dict(spoke_today=False, days_since=None, days_since_signup=None)
    base.update(over)
    return notify._category(**base)


# ── 우선순위 (첫 매치 승리) ──────────────────────────────────

def test_more_chat_beats_ladder():
    assert _cat(spoke_today=True, days_since=30) == push_copy.MORE_CHAT


# ── 미접속 사다리 경계 (활동일 차) ────────────────────────────

def test_ladder_boundaries():
    assert _cat(days_since=0) == push_copy.DEFAULT_RECENT   # 오늘 활동일인데 tokens_used=0인 턴 등
    assert _cat(days_since=1) == push_copy.DEFAULT_RECENT   # 00~04시 대화자는 여기(아래 변환 테스트)
    assert _cat(days_since=2) == push_copy.DEFAULT_RECENT
    assert _cat(days_since=3) == push_copy.DEFAULT_MISSING
    assert _cat(days_since=6) == push_copy.DEFAULT_MISSING
    assert _cat(days_since=7) == push_copy.DEFAULT_LONG
    assert _cat(days_since=100) == push_copy.DEFAULT_LONG


# ── 대화 이력 없음: first_touch vs default_long ───────────────

def test_new_user_gets_first_touch_not_long():
    """가입 당일 유저가 '오랜만이야'를 받는 사고 방지."""
    assert _cat(days_since=None, days_since_signup=0) == push_copy.FIRST_TOUCH
    assert _cat(days_since=None, days_since_signup=notify.FIRST_TOUCH_MAX_DAYS) == push_copy.FIRST_TOUCH


def test_old_never_chatted_user_gets_long():
    assert _cat(days_since=None, days_since_signup=notify.FIRST_TOUCH_MAX_DAYS + 1) == push_copy.DEFAULT_LONG
    assert _cat(days_since=None, days_since_signup=None) == push_copy.DEFAULT_LONG  # created_at 없음


# ── timestamp → 활동일 차 변환 (v1 구멍의 실제 위치) ──────────

class _OneRowSession:
    """execute가 스칼라 1개를 돌려주는 최소 세션 — begin_nested 지원."""

    def __init__(self, value):
        self._value = value

    def begin_nested(self):
        class _CM:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *a):
                return False

        return _CM()

    async def commit(self):  # #16+#24: 푸시 전 트랜잭션 해제 경로
        pass

    async def execute(self, *a, **k):
        value = self._value

        class _Res:
            def scalars(self_inner):
                return self_inner

            def first(self_inner):
                return value

        return _Res()


def _g(tokens_used=0):
    return SimpleNamespace(entitlement={"tokens_remaining": None}, tokens_used=tokens_used)


def test_midnight_chatter_counts_as_one_activity_day():
    """로컬 01시 대화 → 그 발화의 활동일은 어제(04시 경계). 당일 20시엔 1일 차 —
    default_recent로 귀결돼야 한다(v1은 이 유저가 어느 분기에도 안 걸렸다)."""
    last = datetime(2026, 8, 7, 16, 0, tzinfo=timezone.utc)   # KST 8/8 01:00 → 활동일 8/7
    now = datetime(2026, 8, 8, 11, 0, tzinfo=timezone.utc)    # KST 8/8 20:00 → 활동일 8/8
    category, variant = notify._evening_variant(_profile(), _g(), now, True, last)
    assert category == push_copy.DEFAULT_RECENT and variant in push_copy._POOLS[category]


async def test_no_chat_history_is_none_not_failure():
    assert await notify._last_chat_at(_OneRowSession(None), _profile()) == (True, None)


def test_lookup_failure_gives_neutral_fallback():
    now = datetime(2026, 8, 8, 11, 0, tzinfo=timezone.utc)
    assert notify._evening_variant(_profile(), _g(), now, False, None) == ("fallback", push_copy.FALLBACK)


def test_days_since_signup_uses_activity_boundary():
    now = datetime(2026, 8, 8, 11, 0, tzinfo=timezone.utc)  # KST 8/8 20:00 → 활동일 8/8
    early = _profile(created_at=datetime(2026, 8, 7, 16, 0, tzinfo=timezone.utc))  # KST 8/8 01:00 → 8/7
    same_day = _profile(created_at=datetime(2026, 8, 7, 20, 30, tzinfo=timezone.utc))  # KST 8/8 05:30 → 8/8
    assert notify._days_since_signup(early, now) == 1
    assert notify._days_since_signup(same_day, now) == 0
    assert notify._days_since_signup(_profile(), now) is None  # created_at 없음


# ── 신호 실패 폴백 (fail-open) ────────────────────────────────

def _profile(**over):
    p = {"id": uuid.uuid4(), "timezone": "Asia/Seoul", "language": "ko"}
    p.update(over)
    return SimpleNamespace(**p)


_EVENING_NOW = datetime(2026, 8, 7, 11, 0, tzinfo=timezone.utc)  # KST 20:00


def _patch_evening(monkeypatch, sent, claims=None, *, last=(True, None), streak=None, risk_on=False,
                   tokens=("tok",), delivered=1):
    async def _enabled(session, uid, t):
        return True

    async def _claim(session, profile, col, **kw):
        if claims is not None:
            claims.append((col, kw.get("values")))
        return True

    async def _resolve(session, uid, now=None):
        return _g()

    async def _tokens(session, uid):
        return list(tokens)

    async def _send(tokens, title, body, **kw):
        sent.update(title=title, body=body, **kw)
        return delivered

    async def _last(session, profile):
        return last

    async def _push_on(session, key):
        return risk_on

    async def _today(session, profile, now):
        if isinstance(streak, Exception):
            raise streak
        return None if streak is None else notify.RoutineToday(streak, 0)

    monkeypatch.setattr(notify, "_enabled", _enabled)
    monkeypatch.setattr(notify, "_claim_send_slot", _claim)
    monkeypatch.setattr(notify, "_tokens", _tokens)
    monkeypatch.setattr(notify, "_last_chat_at", _last)
    monkeypatch.setattr(notify, "_push_on", _push_on)
    monkeypatch.setattr(notify, "_routine_today", _today)
    monkeypatch.setattr(gating, "resolve", _resolve)
    monkeypatch.setattr(push, "send", _send)
    monkeypatch.setattr(notify, "_record_evening_variant", AsyncMock())


async def test_signal_failure_falls_back_to_neutral_and_still_sends(monkeypatch):
    """마지막 대화 조회가 실패해도 발송은 살고, 문구는 중립(c.fallback)이다."""
    sent, stats = {}, {}
    _patch_evening(monkeypatch, sent, last=(False, None))
    assert await notify.notify_evening(None, _profile(), now=_EVENING_NOW, stats=stats) == 1
    assert (sent["title"], sent["body"]) == push_copy.chat(push_copy.FALLBACK, "ko")
    assert {k: v for k, v in stats.items() if k.startswith("evening_")} == {"evening_fallback": 1}


async def test_category_copy_link_and_variant_when_signals_work(monkeypatch):
    """신호가 정상이면 갈래 문구가 나가고, 대화창으로 이동하며, 문구 id는 FCM 수락 뒤에만 남는다."""
    sent, stats, claims = {}, {}, []
    four_days_ago = _EVENING_NOW - timedelta(days=4)  # 3~6일 → default_missing
    _patch_evening(monkeypatch, sent, claims, last=(True, four_days_ago))
    assert await notify.notify_evening(None, _profile(), now=_EVENING_NOW, stats=stats) == 1
    (col, values), = claims
    assert values == {"evening_push_variant": None}
    notify._record_evening_variant.assert_awaited_once()
    variant = notify._record_evening_variant.await_args.args[3]
    assert col == "evening_notified_at" and variant in push_copy._POOLS[push_copy.DEFAULT_MISSING]
    assert sent["title"] == "BeCappy" and sent["body"] == push_copy.chat_body(variant, "ko")
    assert sent["data"] == {"link": "conversation"} and sent["collapse_key"] == "chat"
    assert sent["expires_at"] == datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)  # 그날 KST 21:00
    assert {k: v for k, v in stats.items() if k.startswith("evening_")} == {"evening_default_missing": 1}


async def test_active_chat_skips_without_claiming(monkeypatch):
    """마지막 대화가 60분 안이면 보내지도 선점하지도 않는다(다음 틱에 다시 본다)."""
    sent, claims = {}, []
    _patch_evening(monkeypatch, sent, claims, last=(True, _EVENING_NOW - timedelta(minutes=59)))
    assert await notify.notify_evening(None, _profile(), now=_EVENING_NOW) == 0
    assert sent == {} and claims == []
    _patch_evening(monkeypatch, sent, claims, last=(True, _EVENING_NOW - timedelta(minutes=61)))
    assert await notify.notify_evening(None, _profile(), now=_EVENING_NOW) == 1


async def test_no_device_token_does_not_claim(monkeypatch):
    """토큰이 없으면 선점·문구 id를 남기지 않는다 — 받지도 않은 푸시가 대화창 첫마디가 되지 않게."""
    sent, claims = {}, []
    _patch_evening(monkeypatch, sent, claims, tokens=())
    assert await notify.notify_evening(None, _profile(), now=_EVENING_NOW) == 0
    assert sent == {} and claims == []


@pytest.mark.parametrize("result", [0, 1, "error"])
async def test_variant_is_exposed_only_after_fcm_acceptance(monkeypatch, result):
    sent, claims, events = {}, [], []
    _patch_evening(monkeypatch, sent, claims)
    record = AsyncMock(side_effect=lambda *args: events.append("record"))
    monkeypatch.setattr(notify, "_record_evening_variant", record)

    async def send(*args, **kwargs):
        assert claims == [("evening_notified_at", {"evening_push_variant": None})]
        record.assert_not_awaited()  # A concurrent greeting sees no variant during send.
        events.append("send")
        if result == "error":
            raise RuntimeError("synthetic send failure")
        return result

    monkeypatch.setattr(push, "send", send)
    if result == "error":
        with pytest.raises(RuntimeError):
            await notify.notify_evening(None, _profile(), now=_EVENING_NOW)
    else:
        assert await notify.notify_evening(None, _profile(), now=_EVENING_NOW) == result
    assert events == (["send", "record"] if result == 1 else ["send"])
    assert record.await_count == int(result == 1)


async def test_just_signed_up_is_treated_as_active(monkeypatch):
    """가입 60분 안이면(첫인사를 보고 있을 때) 대화 푸시를 미룬다 — 온보딩 첫인사가 덮이지 않게."""
    sent, claims = {}, []
    _patch_evening(monkeypatch, sent, claims)
    new_user = _profile(created_at=_EVENING_NOW - timedelta(minutes=10))
    assert await notify.notify_evening(None, new_user, now=_EVENING_NOW) == 0 and claims == []
    old_user = _profile(created_at=_EVENING_NOW - timedelta(hours=2))
    assert await notify.notify_evening(None, old_user, now=_EVENING_NOW) == 1


@pytest.mark.parametrize("hour", [19, 21, 23])
async def test_evening_only_inside_local_20(monkeypatch, hour):
    sent = {}
    _patch_evening(monkeypatch, sent)
    now = datetime(2026, 8, 7, hour - 9, 0, tzinfo=timezone.utc)
    assert await notify.notify_evening(None, _profile(), now=now) == 0 and sent == {}


async def test_streak_risk_beats_chat(monkeypatch):
    """스트릭이 있고 오늘 아직 안 했으면 대화 대신 rr. 대화량 게이트·대화 중 확인을 거치지 않는다."""
    sent, stats, claims = {}, {}, []
    _patch_evening(monkeypatch, sent, claims, streak=8, risk_on=True,
                   last=(True, _EVENING_NOW - timedelta(minutes=5)))

    async def _exhausted(session, uid, now=None):
        return SimpleNamespace(entitlement={"tokens_remaining": 0}, tokens_used=1)

    monkeypatch.setattr(gating, "resolve", _exhausted)
    assert await notify.notify_evening(_OneRowSession(None), _profile(), now=_EVENING_NOW, stats=stats) == 1
    assert (sent["title"], sent["body"]) == ("오늘 안 하면 8일 스트릭이 깨져요 🚨", "지금 루틴 하나만 해도 유지할 수 있어요 🔥")
    assert sent["data"] == {"link": "routine"} and sent["collapse_key"] == "routine"
    assert claims == [("evening_notified_at", {"evening_push_variant": None})]
    assert notify._record_evening_variant.await_args.args[3] == "rr"
    assert {k: v for k, v in stats.items() if k.startswith("evening_")} == {"evening_routine_risk": 1}


@pytest.mark.parametrize("case", ["switch_off", "no_streak", "nothing_today", "error"])
async def test_no_risk_falls_through_to_chat(monkeypatch, case):
    """스위치 꺼짐·스트릭 0·오늘 할 루틴 없음(또는 이미 함)·판단 실패면 대화 푸시."""
    sent = {}
    streak = {"switch_off": 8, "no_streak": 0, "nothing_today": None, "error": RuntimeError("db")}[case]
    _patch_evening(monkeypatch, sent, streak=streak, risk_on=case != "switch_off")
    stats = {}
    assert await notify.notify_evening(_OneRowSession(None), _profile(), now=_EVENING_NOW, stats=stats) == 1
    assert sent["data"] == {"link": "conversation"}
    assert stats.get("routine_check_failed", 0) == (1 if case == "error" else 0)  # 실패는 슬랙 요약에


def test_stat_keys_cover_all_categories_plus_fallback():
    """tick의 counts 초기화가 이 목록으로 만들어진다 — 갈래 추가 시 누락을 구조로 방지."""
    assert set(notify.EVENING_STAT_KEYS) == (
        set(push_copy.CATEGORIES) | {"fallback", notify.OVERRIDE_CATEGORY, notify.ROUTINE_RISK_CATEGORY}
    )


def test_tick_labels_match_stat_keys():
    """슬랙 분포 라벨은 손으로 적은 두 번째 목록 — 갈라지면 그 갈래가 요약에서 조용히 빠진다."""
    from worker import tick

    assert set(tick._EVENING_CATEGORY_KO) == set(notify.EVENING_STAT_KEYS)


# ── app_config 오버라이드 (공지) ──────────────────────────────

def _override_value(date_str: str) -> dict:
    return {
        "date": date_str,
        "ko": ["캐피", "건초 500개가 들어왔어요!"],
        "en": ["Cappy", "500 hay just arrived!"],
        "ja": ["キャピー", "干し草が500個届きました！"],
    }


def _patch_send_pipeline(monkeypatch, sent):
    async def _enabled(session, uid, t):
        return True

    async def _claim(session, profile, col, **kw):
        return True

    async def _tokens(session, uid):
        return ["tok"]

    async def _send(tokens, title, body, **kw):
        sent["title"], sent["body"] = title, body
        return 1

    monkeypatch.setattr(notify, "_enabled", _enabled)
    monkeypatch.setattr(notify, "_claim_send_slot", _claim)
    monkeypatch.setattr(notify, "_tokens", _tokens)
    monkeypatch.setattr(push, "send", _send)
    monkeypatch.setattr(notify, "_record_evening_variant", AsyncMock())


def _patch_override(monkeypatch, value):
    async def _get(session, keys):
        return value

    monkeypatch.setattr(notify, "get_config_values", _get)


def _patch_normal_copy(monkeypatch, called):
    """평소 분기 재개 검증용 — _evening_variant가 실제로 호출됐는지 기록."""
    def _variant(profile, g, now, ok, last):
        called["yes"] = True
        return "more_chat", "c.more.1"

    monkeypatch.setattr(notify, "_evening_variant", _variant)


_NOW = datetime(2026, 8, 14, 11, 0, tzinfo=timezone.utc)  # KST 20:00 → 활동일 8/14
_SESSION = _OneRowSession(None)  # begin_nested만 필요(오버라이드 조회는 monkeypatch)


async def test_override_replaces_copy_and_skips_token_gate(monkeypatch):
    """활동일이 일치하면 오버라이드 문구가 나가고, 대화량 소진 게이트(SOMA-291)를 건너뛴다."""
    sent, stats = {}, {}
    _patch_send_pipeline(monkeypatch, sent)
    _patch_override(monkeypatch, {notify.OVERRIDE_KEY: _override_value("2026-08-14")})

    async def _resolve(session, uid, now=None):
        return SimpleNamespace(entitlement={"tokens_remaining": 0}, tokens_used=0)  # 소진 유저

    monkeypatch.setattr(gating, "resolve", _resolve)

    assert await notify.notify_evening(_SESSION, _profile(), now=_NOW, stats=stats) == 1
    assert (sent["title"], sent["body"]) == ("캐피", "건초 500개가 들어왔어요!")
    assert {k: v for k, v in stats.items() if k.startswith("evening_")} == {"evening_override": 1}


async def test_override_uses_language_bucket(monkeypatch):
    sent = {}
    _patch_send_pipeline(monkeypatch, sent)
    _patch_override(monkeypatch, {notify.OVERRIDE_KEY: _override_value("2026-08-14")})

    assert await notify.notify_evening(_SESSION, _profile(language="ja"), now=_NOW) == 1
    assert (sent["title"], sent["body"]) == ("キャピー", "干し草が500個届きました！")


async def test_override_respects_optout_and_claim(monkeypatch):
    """오버라이드여도 알림 꺼둔 유저·이미 발송한 유저는 그대로 스킵된다."""
    sent = {}
    _patch_send_pipeline(monkeypatch, sent)
    _patch_override(monkeypatch, {notify.OVERRIDE_KEY: _override_value("2026-08-14")})

    async def _disabled(session, uid, t):
        return False

    monkeypatch.setattr(notify, "_enabled", _disabled)
    assert await notify.notify_evening(_SESSION, _profile(), now=_NOW) == 0

    async def _enabled(session, uid, t):
        return True

    async def _no_claim(session, profile, col, **kw):
        return False

    monkeypatch.setattr(notify, "_enabled", _enabled)
    monkeypatch.setattr(notify, "_claim_send_slot", _no_claim)
    assert await notify.notify_evening(_SESSION, _profile(), now=_NOW) == 0
    assert sent == {}


async def test_override_other_date_keeps_normal_behavior(monkeypatch):
    """date가 다른 활동일이면 평소 분기 그대로(게이트 포함) — 예약해둔 공지가 미리 새지 않는다."""
    sent, stats = {}, {}
    _patch_send_pipeline(monkeypatch, sent)
    _patch_override(monkeypatch, {notify.OVERRIDE_KEY: _override_value("2026-08-15")})

    async def _resolve(session, uid, now=None):
        return SimpleNamespace(entitlement={"tokens_remaining": 0}, tokens_used=0)

    monkeypatch.setattr(gating, "resolve", _resolve)

    assert await notify.notify_evening(_SESSION, _profile(), now=_NOW, stats=stats) == 0  # 소진 게이트 유지
    assert sent == {} and stats == {}


async def test_override_absent_key_is_untouched_normal_path(monkeypatch):
    """키가 없으면(평소) 기존 분기가 그대로 돈다 — 100% 무변경 계약."""
    sent, called = {}, {}
    _patch_send_pipeline(monkeypatch, sent)
    _patch_override(monkeypatch, {})
    _patch_normal_copy(monkeypatch, called)

    async def _resolve(session, uid, now=None):
        return SimpleNamespace(entitlement={"tokens_remaining": None}, tokens_used=0)

    monkeypatch.setattr(gating, "resolve", _resolve)

    assert await notify.notify_evening(_SESSION, _profile(), now=_NOW) == 1
    assert called == {"yes": True} and sent["body"] == push_copy.chat_body("c.more.1", "ko")


async def test_override_read_failure_fails_open_to_normal_path(monkeypatch):
    """app_config 조회가 죽어도(20시 피크 DB 장애 등) 평소 분기로 발송이 산다."""
    sent, called = {}, {}
    _patch_send_pipeline(monkeypatch, sent)
    _patch_normal_copy(monkeypatch, called)

    async def _boom(session, keys):
        raise RuntimeError("db down")

    monkeypatch.setattr(notify, "get_config_values", _boom)

    async def _resolve(session, uid, now=None):
        return SimpleNamespace(entitlement={"tokens_remaining": None}, tokens_used=0)

    monkeypatch.setattr(gating, "resolve", _resolve)

    assert await notify.notify_evening(_SESSION, _profile(), now=_NOW) == 1
    assert called == {"yes": True}


async def test_override_malformed_values_fail_open(monkeypatch):
    """운영자 오타(문자열 값·date 누락·언어 누락·잘못된 date)는 전부 평소 분기로."""
    sent, called = {}, {}
    _patch_send_pipeline(monkeypatch, sent)
    _patch_normal_copy(monkeypatch, called)

    async def _resolve(session, uid, now=None):
        return SimpleNamespace(entitlement={"tokens_remaining": None}, tokens_used=0)

    monkeypatch.setattr(gating, "resolve", _resolve)

    bad = _override_value("2026-08-14")
    bad["ko"] = "건초"  # 문자열 — 2글자 (제목,본문) 쪼개짐 사고 방지
    no_date = {k: v for k, v in _override_value("2026-08-14").items() if k != "date"}
    no_lang = {k: v for k, v in _override_value("2026-08-14").items() if k != "ja"}
    for value in ("문자열", bad, no_date, no_lang, _override_value("not-a-date")):
        called.clear()
        _patch_override(monkeypatch, {notify.OVERRIDE_KEY: value})
        assert await notify.notify_evening(_SESSION, _profile(language="ja"), now=_NOW) == 1
        assert called == {"yes": True}, f"fail-open 실패: {value!r}"


@pytest.mark.parametrize('store_fails', [False, True])
async def test_variant_record_failure_does_not_repeat_or_undo_fcm_acceptance(monkeypatch, store_fails):
    record = notify._record_evening_variant
    sent, claims, stats = {}, [], {}
    _patch_evening(monkeypatch, sent, claims)
    monkeypatch.setattr(notify, '_record_evening_variant', record)
    monkeypatch.setattr(notify, '_override_copy', AsyncMock(return_value=None))
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=RuntimeError('synthetic storage failure') if store_fails else None),
        commit=AsyncMock(), rollback=AsyncMock(),
    )
    profile = _profile()
    assert await notify.notify_evening(session, profile, now=_EVENING_NOW, stats=stats) == 1
    assert len(claims) == 1 and claims[0][1] == {'evening_push_variant': None}
    session.execute.assert_awaited_once()
    statement = session.execute.await_args.args[0]
    assert 'user_daily_stats.user_id' in str(statement)
    assert 'user_daily_stats.activity_date' in str(statement)
    assert 'evening_notified_at IS NOT NULL' in str(statement)
    params = statement.compile().params
    assert profile.id in params.values()
    assert any(value == sent['body'] for value in (push_copy.chat_body(v, 'ko') for v in params.values()
               if isinstance(v, str)))
    assert stats['push_sent'] == 1
    assert session.rollback.await_count == int(store_fails)
    # No cleanup writes erase the at-most-once claim after accepted FCM delivery.
