"""워커 배치 틱 — 불량 timezone 하나가 배치 전체를 무너뜨리지 않는지(SOMA-348)."""
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
    def __init__(self, profiles):
        self._profiles = profiles

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt):
        return _Res(self._profiles)

    async def rollback(self):
        pass

    async def commit(self):
        pass


def _fake_get_sessionmaker(profiles):
    def get():
        def maker():
            return _FakeSession(profiles)
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
    # UTC 06:00 = KST 15:00 → 목표 시각(04/09/20) 아님 → 작업 없이 루프만 완주. UTC hour≠4 → sweep 없음.
    now = datetime(2026, 7, 6, 6, 0, tzinfo=timezone.utc)
    counts = await tick.run_tick(now)
    assert counts["users"] == 2  # 예외 없이 두 유저 모두 순회 완료
