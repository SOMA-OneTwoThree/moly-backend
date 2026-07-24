"""배치 워커 틱 — 유저별 세션 격리·불량 tz 스킵·유저 타임아웃(SOMA-348/349)."""
import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from worker import tick


class _Res:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class _FakeSession:
    """execute → 프로필 id 목록(페이지네이션), get → id로 프로필 조회."""
    def __init__(self, ids, by_id):
        self._ids = ids
        self._by_id = by_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt, *a, **k):
        return _Res(self._ids)

    async def get(self, model, pid):
        return self._by_id.get(pid)

    async def rollback(self):
        pass

    async def commit(self):
        pass


def _fake_get_sessionmaker(profiles):
    ids = [p.id for p in profiles]
    by_id = {p.id: p for p in profiles}

    def get():
        def maker():
            return _FakeSession(ids, by_id)
        return maker
    return get


async def test_run_tick_skips_bad_timezone_and_continues(monkeypatch):
    """잘못된 IANA timezone 유저는 스킵하고 나머지 유저 처리를 계속한다(배치 붕괴 방지)."""
    bad = SimpleNamespace(id=uuid.uuid4(), timezone="Not/AZone")
    good = SimpleNamespace(id=uuid.uuid4(), timezone="Asia/Seoul")

    async def _cfg(session):
        return {}

    monkeypatch.setattr(tick, "get_sessionmaker", _fake_get_sessionmaker([bad, good]))
    monkeypatch.setattr(tick, "effective_token_config", _cfg)
    # UTC 06:00 = KST 15:00 → 목표 시각(04/09/20) 아님 → 작업 없이 순회. UTC hour≠4 → sweep 없음.
    now = datetime(2026, 7, 6, 6, 0, tzinfo=timezone.utc)
    counts = await tick.run_tick(now)
    assert counts["users"] == 2 and counts["timed_out"] == 0  # 예외 없이 두 유저 순회


async def test_run_tick_user_timeout_isolated(monkeypatch):
    """한 유저 처리가 타임아웃돼도 배치는 계속되고 timed_out으로 관측된다."""
    p = SimpleNamespace(id=uuid.uuid4(), timezone="Asia/Seoul")

    async def _cfg(session):
        return {}

    async def _slow(now, pid, cfg):
        await asyncio.sleep(0.05)  # 타임아웃 상한 초과
        return {}

    monkeypatch.setattr(tick, "get_sessionmaker", _fake_get_sessionmaker([p]))
    monkeypatch.setattr(tick, "effective_token_config", _cfg)
    monkeypatch.setattr(tick, "_process_user", _slow)
    monkeypatch.setattr(tick.settings, "worker_user_timeout_s", 0.001)
    now = datetime(2026, 7, 6, 6, 0, tzinfo=timezone.utc)
    counts = await tick.run_tick(now)
    assert counts["timed_out"] == 1 and counts["users"] == 1
