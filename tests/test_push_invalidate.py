"""FCM 실패 분류 → 확정 무효 토큰만 user_devices.invalidated_at 표시(드라이런 기본) + 관측 카운트.

FCM 실패 응답의 상태코드(400·401·403·404·429·5xx)만으로 지우면
위험한 경우 — 404(프로젝트 경로 오류, FcmError 없음)·400(페이로드 오류)·403(권한 부족) —
는 전부 토큰을 유지해야 한다. 본문의 FcmError.errorCode가 판정 기준이고, 실제 표시는
settings.fcm_invalidate_codes(기본 UNREGISTERED만)에 든 코드로 한 번 더 좁힌다.
"""
from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace

import pytest

from app.services import gating, notify, push
from worker.tick import _build_summary

UID = uuid.uuid4()
_FCM = "type.googleapis.com/google.firebase.fcm.v1.FcmError"
_BAD = "type.googleapis.com/google.rpc.BadRequest"


def _err(status: int, code: str | None, *, token_field: bool = False, msg: str = "x"):
    details = []
    if code:
        details.append({"@type": _FCM, "errorCode": code})
    if token_field:
        details.append({"@type": _BAD, "fieldViolations": [{"field": "message.token", "description": "Invalid registration token"}]})
    return {"error": {"code": status, "message": msg, "status": "ERR", "details": details}}


# ── classify_failure (순수 함수) ─────────────────────────────────────────────

def test_unregistered_404_is_invalid_token():
    assert push.classify_failure(404, _err(404, "UNREGISTERED")) == (push.INVALID_TOKEN, "UNREGISTERED")


def test_404_without_fcm_error_is_setup_error_not_token():
    """프로젝트 경로 오타 등 — 전 토큰이 404라도 표시하면 안 된다(전원 푸시 중단 방지)."""
    body = {"error": {"code": 404, "message": "Requested entity was not found.", "status": "NOT_FOUND"}}
    assert push.classify_failure(404, body)[0] == push.SETUP_ERROR
    assert push.classify_failure(404, None)[0] == push.SETUP_ERROR


def test_400_is_invalid_token_only_when_token_field_violated():
    assert push.classify_failure(400, _err(400, "INVALID_ARGUMENT", token_field=True))[0] == push.INVALID_TOKEN
    # 페이로드 오류(토큰 필드가 아닌 400 — 우리 메시지 버그 신호) → 토큰 유지
    assert push.classify_failure(400, _err(400, "INVALID_ARGUMENT"))[0] == push.PAYLOAD_ERROR
    assert push.classify_failure(400, None)[0] == push.PAYLOAD_ERROR


def test_403_sender_mismatch_is_invalid_but_permission_denied_is_setup():
    assert push.classify_failure(403, _err(403, "SENDER_ID_MISMATCH"))[0] == push.INVALID_TOKEN
    assert push.classify_failure(403, {"error": {"code": 403, "status": "PERMISSION_DENIED"}})[0] == push.SETUP_ERROR


def test_401_apns_and_5xx_429_keep_token():
    assert push.classify_failure(401, _err(401, "THIRD_PARTY_AUTH_ERROR"))[0] == push.SETUP_ERROR
    assert push.classify_failure(500, _err(500, "INTERNAL"))[0] == push.TRANSIENT
    assert push.classify_failure(503, _err(503, "UNAVAILABLE"))[0] == push.TRANSIENT
    assert push.classify_failure(429, _err(429, "QUOTA_EXCEEDED"))[0] == push.TRANSIENT


# ── send(): 결과 객체는 int 호환 + 허용 코드의 무효 토큰 목록 ───────────────

class _Resp:
    def __init__(self, status, body=None, raw=False):
        self.status_code = status
        self._body = body
        self._raw = raw

    def json(self):
        if self._raw:
            raise ValueError("not json")
        return self._body


def _client(responses):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, **kwargs):
            return responses.pop(0)

    return Client


def _responses():
    return [
        _Resp(200),
        _Resp(404, _err(404, "UNREGISTERED")),
        _Resp(403, _err(403, "SENDER_ID_MISMATCH")),
        _Resp(400, _err(400, "INVALID_ARGUMENT")),  # 페이로드 오류 → 유지
        _Resp(404, {"error": {"code": 404, "status": "NOT_FOUND"}}),  # 프로젝트 오류 → 유지
        _Resp(500, None, raw=True),
    ]


async def test_send_default_allowlist_marks_only_unregistered(monkeypatch):
    monkeypatch.setattr(push.settings, "fcm_project_id", "p")
    monkeypatch.setattr(push.settings, "fcm_invalidate_codes", "UNREGISTERED")
    monkeypatch.setattr(push, "_access_token", lambda: "fake")
    monkeypatch.setattr(push.httpx, "AsyncClient", lambda **kw: _client(_responses())())
    r = await push.send(["ok", "dead", "foreign", "payload", "proj", "boom"], "t", "b")
    assert r == 1 and isinstance(r, int) and bool(r)  # 기존 호출자의 int 계약 유지
    assert r.invalid_tokens == ("dead",)  # 403 SENDER_ID_MISMATCH는 분류만, 표시 대상 아님(기본)
    assert r.failures == {
        push.INVALID_TOKEN: 2, push.PAYLOAD_ERROR: 1, push.SETUP_ERROR: 1, push.TRANSIENT: 1,
    }


async def test_send_allowlist_can_be_widened_after_body_review(monkeypatch):
    monkeypatch.setattr(push.settings, "fcm_project_id", "p")
    monkeypatch.setattr(push.settings, "fcm_invalidate_codes", "UNREGISTERED, sender_id_mismatch")
    monkeypatch.setattr(push, "_access_token", lambda: "fake")
    monkeypatch.setattr(push.httpx, "AsyncClient", lambda **kw: _client(_responses())())
    r = await push.send(["ok", "dead", "foreign", "payload", "proj", "boom"], "t", "b")
    assert r.invalid_tokens == ("dead", "foreign")


async def test_send_without_tokens_or_credentials_returns_zero_result(monkeypatch):
    assert (await push.send([], "t", "b")).invalid_tokens == ()
    monkeypatch.setattr(push.settings, "fcm_project_id", "")
    assert await push.send(["tok"], "t", "b") == 0


# ── notify: 표시는 플래그가 켜졌을 때만, (user_id, push_token) 쌍으로, UPDATE로 ──────

class _Session:
    def __init__(self):
        self.executed = []
        self.commits = 0

    async def execute(self, stmt):
        self.executed.append(stmt)
        return SimpleNamespace(rowcount=1, scalars=lambda: SimpleNamespace(all=lambda: [], first=lambda: None))

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


def _writes(session):
    return [str(x) for x in session.executed if str(x).upper().startswith(("UPDATE", "DELETE"))]


def _evening_env(monkeypatch, result):
    async def _enabled(session, uid, t):
        return True

    async def _claim(session, profile, col):
        return True

    async def _resolve(session, uid, now=None):
        return SimpleNamespace(entitlement={"tokens_remaining": None}, tokens_used=0)

    async def _tokens(session, uid):
        return ["live", "dead"]

    async def _override(session, profile, now):
        return None

    async def _send(tokens, title, body):
        return result

    monkeypatch.setattr(notify, "_enabled", _enabled)
    monkeypatch.setattr(notify, "_claim_send_slot", _claim)
    monkeypatch.setattr(notify, "_tokens", _tokens)
    monkeypatch.setattr(notify, "_override_copy", _override)
    monkeypatch.setattr(gating, "resolve", _resolve)
    monkeypatch.setattr(push, "send", _send)


async def test_evening_dry_run_counts_but_does_not_write(monkeypatch):
    monkeypatch.setattr(notify.settings, "fcm_invalidate_dead_tokens", False)
    _evening_env(monkeypatch, push.SendResult(1, ["dead"], {push.INVALID_TOKEN: 1}))
    s = _Session()
    stats = {}
    assert await notify.notify_evening(s, SimpleNamespace(id=UID, timezone="Asia/Seoul", language="ko"), stats=stats) == 1
    assert _writes(s) == []
    assert stats["push_sent"] == 1 and stats["push_invalid_token"] == 1 and "push_invalidated" not in stats


async def test_evening_marks_only_this_users_dead_tokens_with_update(monkeypatch):
    monkeypatch.setattr(notify.settings, "fcm_invalidate_dead_tokens", True)
    _evening_env(monkeypatch, push.SendResult(1, ["dead"], {push.INVALID_TOKEN: 1}))
    s = _Session()
    stats = {}
    assert await notify.notify_evening(s, SimpleNamespace(id=UID, timezone="Asia/Seoul", language="ko"), stats=stats) == 1
    writes = _writes(s)
    assert len(writes) == 1 and writes[0].startswith("UPDATE user_devices SET invalidated_at=now()")
    assert "user_devices.user_id" in writes[0] and "user_devices.push_token IN" in writes[0]
    assert "DELETE" not in writes[0]
    assert stats["push_invalidated"] == 1
    assert s.commits >= 1


async def test_evening_int_fake_from_legacy_tests_still_works(monkeypatch):
    """기존 테스트의 int fake(push.send → 1) 호환 — 속성 없으면 표시·분류 생략."""
    monkeypatch.setattr(notify.settings, "fcm_invalidate_dead_tokens", True)
    _evening_env(monkeypatch, 1)
    s = _Session()
    stats = {}
    assert await notify.notify_evening(s, SimpleNamespace(id=UID, timezone="Asia/Seoul", language="ko"), stats=stats) == 1
    assert _writes(s) == []
    assert stats["push_sent"] == 1


async def test_morning_marks_dead_tokens_too(monkeypatch):
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock

    monkeypatch.setattr(notify.settings, "morning_push_enabled", True)
    monkeypatch.setattr(notify.settings, "fcm_invalidate_dead_tokens", True)
    monkeypatch.setattr(notify, "_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(notify, "_morning_diary_id", AsyncMock(return_value=uuid.uuid4()))
    monkeypatch.setattr(notify, "_tokens", AsyncMock(return_value=["dead1", "dead2"]))
    monkeypatch.setattr(notify, "_claim_send_slot", AsyncMock(return_value=True))
    monkeypatch.setattr(push, "prepare_access_token", AsyncMock(return_value="acc"))
    monkeypatch.setattr(push, "send", AsyncMock(return_value=push.SendResult(0, ["dead1", "dead2"], {push.INVALID_TOKEN: 2})))
    s = _Session()
    stats = {}
    n = await notify.notify_morning(
        s, SimpleNamespace(id=UID, timezone="Asia/Seoul", language="ko"),
        now=datetime(2026, 1, 15, 0, tzinfo=timezone.utc), stats=stats,
    )
    assert n == 0  # 전 토큰 무효 → '미전달' 경고는 그대로, 행은 표시만
    assert len(_writes(s)) == 1 and _writes(s)[0].startswith("UPDATE user_devices")
    assert stats["push_invalid_token"] == 2 and stats["push_invalidated"] == 1  # rowcount fake=1


async def test_tokens_query_skips_invalidated_unless_reregistered():
    """표시된 행은 건너뛰되, 그 뒤 재등록(last_active_at 갱신)된 행은 되살린다."""
    s = _Session()
    assert await notify._tokens(s, UID) == []
    sql = str(s.executed[0])
    assert "user_devices.invalidated_at IS NULL" in sql
    assert "user_devices.last_active_at > user_devices.invalidated_at" in sql


# ── 슬랙 요약 ─────────────────────────────────────────────────────────────────

def _counts(**over):
    base = {
        "diaries": 0, "diary_llm": 0, "diary_preset": 0, "diary_none": 0, "diary_failed": 0,
        "morning": 0, "evening": 350, "users": 700,
    }
    base.update(over)
    return base


def test_summary_shows_failure_line_only_when_failures_exist():
    from datetime import datetime, timezone

    now = datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc)
    quiet = _build_summary(now, _counts(), elapsed=1.0)
    assert "푸시 실패" not in quiet
    loud = _build_summary(
        now, _counts(push_invalid_token=12, push_invalidated=10, push_transient=2, push_setup_error=1), elapsed=1.0,
    )
    assert "푸시 실패: 무효 토큰 12건(비활성 10) / 기타 2건 / ⚠️ 설정·인증 오류 1건" in loud


@pytest.mark.parametrize("per_user,warned", [
    ({"push_invalid_token": 6, "push_invalidated": 6}, True),  # 2명 합산 12건, 수락 0 → 공통 원인 의심
    ({"push_invalid_token": 6, "push_sent": 1}, False),  # 수락이 있으면 개별 토큰 문제
    ({"push_invalid_token": 4}, False),  # 하한(10) 미만 — 작은 틱에서 죽은 토큰 몇 개
])
async def test_tick_warns_when_nothing_accepted_but_many_tokens_invalid(monkeypatch, caplog, per_user, warned):
    """틱 전역 신호는 로그만 남긴다(경보 정책 불변) — 드라이런에서도 같은 조건으로 보인다."""
    from datetime import datetime, timezone

    from tests.test_worker_tick import _fake_get_sessionmaker
    from worker import tick

    async def _cfg(session):
        return {}

    async def _work(now, pid, cfg, *, diary_policy=None):
        return dict(per_user)

    profiles = [SimpleNamespace(id=uuid.uuid4(), timezone="Asia/Seoul") for _ in range(2)]
    monkeypatch.setattr(tick, "get_sessionmaker", _fake_get_sessionmaker(profiles))
    monkeypatch.setattr(tick, "effective_token_config", _cfg)
    monkeypatch.setattr(tick, "_process_user", _work)
    caplog.set_level(logging.WARNING, logger="moly-worker")
    # UTC 11:00 = KST 20:00 — 저녁 틱(유저 루프가 실제로 돈다).
    await tick.run_tick(datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc))
    expected = "FCM 수락 0건인데 무효 토큰 12건(비활성 표시 12건)"
    assert (expected in caplog.text) is warned
    assert ("FCM 수락 0건인데" in caplog.text) is warned


# ── 런북 고정: docs/OPERATIONS.md의 grep 문자열 ↔ 로그 포맷, 분류 ↔ 카운트 키 ─────────

async def test_runbook_grep_strings_match_log_lines(monkeypatch, caplog):
    """OPERATIONS.md 드라이런 확인의 journalctl grep 문자열이 실제 로그 줄에 그대로 나와야 한다."""
    caplog.set_level(logging.INFO, logger="moly-worker")
    monkeypatch.setattr(push.settings, "fcm_project_id", "p")
    monkeypatch.setattr(push.settings, "fcm_invalidate_codes", "UNREGISTERED")
    monkeypatch.setattr(push, "_access_token", lambda: "fake")
    monkeypatch.setattr(push.httpx, "AsyncClient", lambda **kw: _client(_responses())())
    r = await push.send(["ok", "dead", "foreign", "payload", "proj", "boom"], "t", "b")
    monkeypatch.setattr(notify.settings, "fcm_invalidate_dead_tokens", False)
    await notify._invalidate_dead_tokens(_Session(), UID, r.invalid_tokens, stats={})
    for needle in ("HTTP 404 UNREGISTERED", "[payload_error]", "[setup_error]", "비활성 표시 꺼짐"):
        assert needle in caplog.text
    assert "HTTP 403 SENDER_ID_MISMATCH — 무효 토큰(표시 안 함)" in caplog.text


def test_failure_kinds_have_stat_keys():
    """push_{분류}가 전부 PUSH_STAT_KEYS에 있어야 tick 병합(`k in counts`)에서 버려지지 않는다."""
    assert {f"push_{k}" for k in push.FAILURE_KINDS} <= set(notify.PUSH_STAT_KEYS)
