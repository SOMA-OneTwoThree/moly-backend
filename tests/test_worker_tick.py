"""배치 워커 틱 — 유저별 세션 격리·불량 tz 스킵·유저 타임아웃(SOMA-348/349)."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
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


async def test_run_tick_records_heartbeat_every_tick(monkeypatch):
    """하트비트(worker_last_success)는 DIARY_HOUR가 아닌 틱에서도 기록된다.

    회귀 방지: SOMA-349 머지 충돌 해소에서 기록이 DIARY_HOUR 블록 안으로 들어가
    하루 1회만 갱신 → /health/deep이 하루 22시간 오탐 stale(503)이 됐던 버그.
    """
    p = SimpleNamespace(id=uuid.uuid4(), timezone="Asia/Seoul")
    recorded: list[str] = []

    async def _cfg(session):
        return {}

    async def _spy(session, key, value):
        recorded.append(key)

    monkeypatch.setattr(tick, "get_sessionmaker", _fake_get_sessionmaker([p]))
    monkeypatch.setattr(tick, "effective_token_config", _cfg)
    monkeypatch.setattr(tick.config_store, "set_config_value", _spy)
    # UTC 06:00 → DIARY_HOUR(04) 아님 — 그래도 하트비트는 기록돼야 한다
    now = datetime(2026, 7, 6, 6, 0, tzinfo=timezone.utc)
    await tick.run_tick(now)
    assert tick.config_store.WORKER_LAST_SUCCESS_KEY in recorded


def test_rc_inbox_drain_priority_ordering():
    """우선순위 후보 정렬(SOMA-372): 신규(last_error NULL)→예외재시도(attempts>0)→received_at.

    실제 발행되는 select의 ORDER BY를 컴파일해 세 우선순위가 이 순서로 존재함을 검증한다.
    """
    sql = str(tick._priority_drain_stmt().compile(compile_kwargs={"literal_binds": True})).lower()
    order = sql[sql.index("order by"):]
    i_new = order.index("last_error is null")     # 1) 신규 우선
    i_retry = order.index("attempts > 0")          # 2) 예외 재시도
    i_recv = order.index("received_at")            # 3) received_at(오래된 dependency 순)
    assert i_new < i_retry < i_recv
    assert "desc" in order[i_new:i_retry]          # 신규(NULL)가 앞서도록 DESC
    assert "desc" in order[i_retry:i_recv]         # 예외재시도(attempts>0)가 앞서도록 DESC


class _InboxSim:
    """RC inbox 드레인 공정성 시뮬레이터 — _select_pending_ids의 두 쿼리를 실 SQL 의미대로 답한다.

    call 1 = dependency_quota_stmt(오래된 dependency 예약분).
    call 2 = priority_drain_stmt(신규 우선 → 남으면 dependency, 배치 상한).
    """
    def __init__(self, dep_pool, new_pool):
        self.dep_pool = list(dep_pool)   # 오래된 순
        self.new_pool = list(new_pool)   # 신규(우선순위 상위)
        self._call = 0

    async def execute(self, stmt):
        self._call += 1
        if self._call == 1:  # dependency_quota_stmt → 예약분
            return _Res(self.dep_pool[:tick._RC_DEP_RESERVED])
        # priority_drain_stmt → 신규 먼저(배치 가득) → 남으면 dependency
        return _Res((self.new_pool + self.dep_pool)[:tick._RC_INBOX_BATCH])


async def test_select_pending_ids_reserves_dependency_quota():
    """신규 다수 + dependency 소수: 신규가 배치를 가득 채워도 오래된 dependency가 예약 슬롯만큼 선택된다."""
    deps = [f"dep-{i}" for i in range(5)]                       # 소수 dependency
    news = [f"new-{i}" for i in range(tick._RC_INBOX_BATCH)]    # 신규가 배치를 초과 점유
    selected = await tick._select_pending_ids(_InboxSim(deps, news))
    assert set(deps) <= set(selected)               # dependency 전부 포함(굶지 않음)
    assert any(s.startswith("new-") for s in selected)  # 신규도 굶지 않음
    assert len(selected) <= tick._RC_INBOX_BATCH     # 배치 상한 준수


async def test_select_pending_ids_drains_dependency_despite_new_flood():
    """신규가 매 틱 배치를 초과 유입해도 여러 틱 내 모든 dependency가 처리된다(strict priority였다면 굶음)."""
    deps = [f"dep-{i}" for i in range(tick._RC_DEP_RESERVED + 30)]  # 예약분보다 많은 dependency
    remaining = list(deps)
    processed: set[str] = set()
    for t in range(20):  # 여러 틱
        news = [f"new-{t}-{i}" for i in range(tick._RC_INBOX_BATCH)]  # 매 틱 배치 초과 유입
        selected = await tick._select_pending_ids(_InboxSim(remaining, news))
        assert any(s.startswith("new-") for s in selected)  # 신규도 굶지 않음
        for d in [x for x in selected if x.startswith("dep-")]:  # 선택된 dependency는 처리됨
            processed.add(d)
            if d in remaining:
                remaining.remove(d)
        if not remaining:
            break
    assert not remaining and set(deps) == processed  # 모든 dependency가 결국 처리됨


class _RotationSim:
    """dependency 내부 rotation 시뮬레이터 — next_attempt_at 순서·(<= now) 필터를 실 SQL 의미대로 답한다.

    call 1 = dependency_quota_stmt(예약분), call 2 = priority_drain_stmt(배치). 둘 다 next_attempt_at 이른 순.
    선택 즉시 제거가 아니라, 재-pending 시 next_attempt_at를 뒤로 미는 것으로 rotation을 재현한다.
    """
    def __init__(self, next_at: dict, now: datetime):
        self.next_at = next_at
        self.now = now
        self._call = 0

    async def execute(self, stmt):
        self._call += 1
        eligible = sorted(
            (d for d, t in self.next_at.items() if t <= self.now),
            key=lambda d: (self.next_at[d], d),   # next_attempt_at 이른 순(동률 안정)
        )
        limit = tick._RC_DEP_RESERVED if self._call == 1 else tick._RC_INBOX_BATCH
        return _Res(eligible[:limit])


async def test_dependency_internal_rotation_via_next_attempt_at():
    """dependency 집합이 배치를 초과해도 재-pending 시 next_attempt_at가 밀려(backoff) 여러 틱에 걸쳐
    전부 최소 1회 선택된다(SOMA-372 집합 내부 rotation). received_at 고정 정렬이면 초과분은 영구 미선택."""
    base = datetime(2026, 7, 28, 0, 0, tzinfo=timezone.utc)
    backoff = timedelta(minutes=5)
    n = tick._RC_INBOX_BATCH + 50                       # 배치를 초과하는 dependency
    next_at = {f"dep-{i}": base for i in range(n)}      # 전부 동일 도착시각(next_attempt=received 근사)
    selected_ever: set[str] = set()
    for k in range(10):                                 # 여러 틱(15분 케이던스)
        cur = base + timedelta(minutes=15 * k)
        selected = await tick._select_pending_ids(_RotationSim(next_at, cur))
        assert selected                                 # 매 틱 무언가 선택
        selected_ever.update(selected)
        for d in selected:                              # 재-pending: next_attempt_at 뒤로 밀림
            next_at[d] = cur + backoff
        if len(selected_ever) == n:
            break
    assert selected_ever == set(next_at)                # 모든 dependency가 결국 선택(굶지 않음)
