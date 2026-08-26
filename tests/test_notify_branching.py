"""저녁 푸시 분기 사다리 계약 — 우선순위·경계·신규·폴백.

사다리는 순수 함수(notify._category)라 mock 없이 고정한다. 날짜 규약은 전 분기
활동일(로컬 04시 경계) 통일 — 00~04시 대화자가 어느 분기에도 안 걸리던 v1 구멍의 방지책.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from app.services import gating, notify, push, push_copy


def _cat(**over):
    base = dict(spoke_today=False, has_unread_diary=False, days_since=None, days_since_signup=None)
    base.update(over)
    return notify._category(**base)


# ── 우선순위 (첫 매치 승리) ──────────────────────────────────

def test_more_chat_beats_diary_teaser():
    """매일 대화+일기 안 읽는 유저가 티저에 갇히지 않는다 — 순서 뒤집기의 존재 이유."""
    assert _cat(spoke_today=True, has_unread_diary=True) == push_copy.MORE_CHAT


def test_teaser_fires_for_non_chatter_with_unread_diary():
    assert _cat(has_unread_diary=True, days_since=3) == push_copy.DIARY_TEASER


def test_teaser_beats_ladder():
    assert _cat(has_unread_diary=True, days_since=30) == push_copy.DIARY_TEASER


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

    async def execute(self, *a, **k):
        value = self._value

        class _Res:
            def scalars(self_inner):
                return self_inner

            def first(self_inner):
                return value

        return _Res()


async def test_midnight_chatter_counts_as_one_activity_day():
    """로컬 01시 대화 → 그 발화의 활동일은 어제(04시 경계). 당일 20시엔 days_since=1 —
    default_recent로 귀결돼야 한다(v1은 이 유저가 어느 분기에도 안 걸렸다)."""
    last = datetime(2026, 8, 7, 16, 0, tzinfo=timezone.utc)   # KST 8/8 01:00 → 활동일 8/7
    now = datetime(2026, 8, 8, 11, 0, tzinfo=timezone.utc)    # KST 8/8 20:00 → 활동일 8/8
    ok, days = await notify._days_since_last_chat(_OneRowSession(last), _profile(), now)
    assert (ok, days) == (True, 1)
    assert _cat(days_since=days) == push_copy.DEFAULT_RECENT


async def test_no_chat_history_is_none_not_failure():
    ok, days = await notify._days_since_last_chat(_OneRowSession(None), _profile(), now=datetime(2026, 8, 8, 11, 0, tzinfo=timezone.utc))
    assert (ok, days) == (True, None)


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


async def test_signal_failure_falls_back_to_neutral_and_still_sends(monkeypatch):
    """session=None(신호 조회 전부 실패)이어도 발송은 살고, 문구는 중립(_EVENING)이다.

    클레임이 이미 커밋된 뒤라 발송 포기는 그날 푸시의 조용한 소멸 — 금지 계약.
    """
    sent, stats = {}, {}

    async def _enabled(session, uid, t):
        return True

    async def _claim(session, profile, col):
        return True

    async def _resolve(session, uid, now=None):
        return SimpleNamespace(entitlement={"tokens_remaining": None}, tokens_used=0)

    async def _tokens(session, uid):
        return ["tok"]

    async def _send(tokens, title, body):
        sent["title"], sent["body"] = title, body
        return 1

    monkeypatch.setattr(notify, "_enabled", _enabled)
    monkeypatch.setattr(notify, "_claim_send_slot", _claim)
    monkeypatch.setattr(notify, "_tokens", _tokens)
    monkeypatch.setattr(gating, "resolve", _resolve)
    monkeypatch.setattr(push, "send", _send)

    assert await notify.notify_evening(None, _profile(), stats=stats) == 1
    assert (sent["title"], sent["body"]) == notify._EVENING["ko"]  # 중립 폴백
    assert stats == {"evening_fallback": 1}


async def test_category_copy_and_stats_when_signals_work(monkeypatch):
    """신호가 정상이면 카테고리 풀의 문구가 나가고 stats에 그 카테고리가 찍힌다."""
    sent, stats = {}, {}

    async def _enabled(session, uid, t):
        return True

    async def _claim(session, profile, col):
        return True

    async def _resolve(session, uid, now=None):
        return SimpleNamespace(entitlement={"tokens_remaining": None}, tokens_used=0)

    async def _tokens(session, uid):
        return ["tok"]

    async def _send(tokens, title, body):
        sent["body"] = body
        return 1

    async def _unread(session, profile, now):
        return False

    async def _days(session, profile, now):
        return True, 4  # 3~6일 → default_missing

    monkeypatch.setattr(notify, "_enabled", _enabled)
    monkeypatch.setattr(notify, "_claim_send_slot", _claim)
    monkeypatch.setattr(notify, "_tokens", _tokens)
    monkeypatch.setattr(notify, "_unread_yesterday_diary", _unread)
    monkeypatch.setattr(notify, "_days_since_last_chat", _days)
    monkeypatch.setattr(gating, "resolve", _resolve)
    monkeypatch.setattr(push, "send", _send)

    now = datetime(2026, 8, 7, 11, 0, tzinfo=timezone.utc)  # KST 20:00
    assert await notify.notify_evening(None, _profile(), now=now, stats=stats) == 1
    pool_bodies = {b for _, b in push_copy._POOLS[push_copy.DEFAULT_MISSING]["ko"]}
    assert sent["body"] in pool_bodies
    assert stats == {"evening_default_missing": 1}


def test_stat_keys_cover_all_categories_plus_fallback():
    """tick의 counts 초기화가 이 목록으로 만들어진다 — 카테고리 추가 시 누락을 구조로 방지."""
    assert set(notify.EVENING_STAT_KEYS) == (
        set(push_copy.CATEGORIES) | {"fallback", notify.OVERRIDE_CATEGORY}
    )


def test_tick_labels_match_stat_keys():
    """슬랙 분포 라벨은 손으로 적은 두 번째 목록 — 갈라지면 그 카테고리가 요약에서 조용히 빠진다."""
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

    async def _claim(session, profile, col):
        return True

    async def _tokens(session, uid):
        return ["tok"]

    async def _send(tokens, title, body):
        sent["title"], sent["body"] = title, body
        return 1

    monkeypatch.setattr(notify, "_enabled", _enabled)
    monkeypatch.setattr(notify, "_claim_send_slot", _claim)
    monkeypatch.setattr(notify, "_tokens", _tokens)
    monkeypatch.setattr(push, "send", _send)


def _patch_override(monkeypatch, value):
    async def _get(session, keys):
        return value

    monkeypatch.setattr(notify, "get_config_values", _get)


def _patch_normal_copy(monkeypatch, called):
    """평소 분기 재개 검증용 — _evening_copy가 실제로 호출됐는지 기록."""
    async def _copy(session, profile, g, now):
        called["yes"] = True
        return "more_chat", "제목", "본문"

    monkeypatch.setattr(notify, "_evening_copy", _copy)


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
    assert stats == {"evening_override": 1}


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

    async def _no_claim(session, profile, col):
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
    assert called == {"yes": True} and sent["body"] == "본문"


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
