"""SOMA-373: 워커 틱 중첩 시 (유저,날짜) advisory lock으로 일기 중복 LLM 생성 방지."""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from worker import tick

PID = uuid.uuid4()
_NOW = datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc)  # UTC 04:00 = DIARY_HOUR
_CFG = {"diary_min_user_chars": 60}


class _ScalarRes:
    def __init__(self, v):
        self._v = v

    def scalar(self):
        return self._v


class _LeaseSession:
    def __init__(self, lock_granted):
        self._granted = lock_granted
        self.executed: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, model, pid):
        return SimpleNamespace(id=PID, timezone="UTC", language="ko", nickname=None)

    async def execute(self, stmt, params=None):
        s = str(stmt)
        self.executed.append(s)
        return _ScalarRes(self._granted if "try_advisory" in s else None)

    async def rollback(self):
        pass

    async def commit(self):
        pass


def _patch_session(monkeypatch, sess):
    monkeypatch.setattr(tick, "get_sessionmaker", lambda: lambda: sess)


async def test_diary_lease_skips_when_lock_not_acquired(monkeypatch):
    """다른 프로세스가 락을 쥐고 있으면(pg_try_advisory_lock=False) 생성하지 않고 스킵한다."""
    called = {"gen": False}

    async def _gen(*a, **k):
        called["gen"] = True
        return {"created": True, "source": "llm"}

    monkeypatch.setattr(tick.diary_generation, "generate_for_user", _gen)
    _patch_session(monkeypatch, _LeaseSession(lock_granted=False))
    out = await tick._process_user(_NOW, PID, _CFG)
    assert called["gen"] is False           # LLM 호출 안 함(중복 방지)
    assert out["diary_skipped"] == 1 and out["diaries"] == 0


async def test_diary_lease_proceeds_and_unlocks_when_acquired(monkeypatch):
    """락을 얻으면 생성하고, 끝나면 반드시 언락한다."""
    called = {"gen": False}

    async def _gen(session, p, target, cfg):
        called["gen"] = True
        return {"created": True, "source": "llm", "memory_ok": 1}

    monkeypatch.setattr(tick.diary_generation, "generate_for_user", _gen)
    sess = _LeaseSession(lock_granted=True)
    _patch_session(monkeypatch, sess)
    out = await tick._process_user(_NOW, PID, _CFG)
    assert called["gen"] is True and out["diaries"] == 1
    assert any("advisory_unlock" in s for s in sess.executed)  # 락 해제됨
