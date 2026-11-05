"""워커 틱 소프트 데드라인 — 하드킬 전에 스스로 멈추고 후처리는 살린다, 그 시각의 마지막 틱은 끝까지."""
import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_worker_tick import _fake_get_sessionmaker
from worker import tick


class _GetClient:
    urls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url):
        _GetClient.urls.append(url)


@pytest.fixture(autouse=True)
def _quiet_tick(monkeypatch):
    monkeypatch.setattr(tick.diary_generation, "load_policy",
                        AsyncMock(return_value=tick.diary_generation.DiaryPolicy()))
    monkeypatch.setattr(tick, "effective_token_config", AsyncMock(return_value={}))
    monkeypatch.setattr(tick, "_drain_rc_inbox", AsyncMock(return_value={}))
    monkeypatch.setattr(tick.slack_notify, "send_summary", AsyncMock())
    monkeypatch.setattr(tick.monitoring, "emit", AsyncMock())
    monkeypatch.setattr(tick.slack_notify, "alert", AsyncMock())
    monkeypatch.setattr(tick.config_store, "set_config_value", AsyncMock())
    monkeypatch.setattr(tick.memory_pipeline, "enqueue_memory_sweep", AsyncMock())
    monkeypatch.setattr(tick.settings, "worker_ping_url", "")
    monkeypatch.setattr(tick.settings, "daily_billable_alert_threshold", 0)
    from worker import retention_jobs
    monkeypatch.setattr(retention_jobs, "enqueue_daily", AsyncMock(return_value=0))


def _users(monkeypatch, *, n=3, tz="Asia/Seoul", result=None):
    profiles = [SimpleNamespace(id=uuid.uuid4(), timezone=tz) for _ in range(n)]
    monkeypatch.setattr(tick, "get_sessionmaker", _fake_get_sessionmaker(profiles))
    seen = []

    async def _work(now, pid, cfg, *, diary_policy=None):
        seen.append(pid)
        await asyncio.sleep(0.05)
        return dict(result or {"diaries": 1})

    monkeypatch.setattr(tick, "_process_user", _work)
    return seen


async def test_soft_deadline_skips_rest_and_post_loop_still_runs(monkeypatch, caplog):
    caplog.set_level("INFO", logger="moly-worker")
    seen = _users(monkeypatch)
    monkeypatch.setattr(tick.settings, "worker_tick_soft_deadline_s", 0.01)
    monkeypatch.setattr(tick.settings, "worker_ping_url", "https://hc.example/PING")
    monkeypatch.setattr(tick.httpx, "AsyncClient", lambda **kw: _GetClient())
    _GetClient.urls.clear()
    # UTC 19:00 = KST 04:00 — 같은 시각에 04:15·04:30·04:45 틱이 남아 있다(이어받을 틱 있음).
    counts = await tick.run_tick(datetime(2026, 10, 5, 19, 0, tzinfo=timezone.utc))
    assert len(seen) == 1
    assert counts["diaries"] == 1 and counts["deadline_skipped"] == 2
    keys = [c.args[1] for c in tick.config_store.set_config_value.await_args_list]
    assert tick.config_store.WORKER_LAST_SUCCESS_KEY in keys  # 루프 뒤 단계(heartbeat)가 돌았다
    assert "소프트 데드라인 도달 — 미처리 2명(다음 틱 인계)" in caplog.text
    tick.slack_notify.send_summary.assert_not_awaited()
    # 용량 관측은 로그·일일 상태로 남기고, 실패 경보·데드맨 /fail로 올리지 않는다.
    tick.slack_notify.alert.assert_not_awaited()
    assert _GetClient.urls == ["https://hc.example/PING"]


async def test_soft_deadline_alone_still_logs_summary(monkeypatch, caplog):
    """처리 건수가 0이어도 데드라인 도달을 상세 로그로 남긴다."""
    caplog.set_level("INFO", logger="moly-worker")
    _users(monkeypatch, result={"diary_none": 1})
    monkeypatch.setattr(tick.settings, "worker_tick_soft_deadline_s", 0.01)
    counts = await tick.run_tick(datetime(2026, 10, 5, 19, 0, tzinfo=timezone.utc))
    assert counts["diaries"] == 0 and counts["deadline_skipped"] == 2
    tick.slack_notify.send_summary.assert_not_awaited()
    assert "소프트 데드라인 도달 — 미처리 2명(다음 틱 인계)" in caplog.text


async def test_last_tick_gets_extra_time_over_soft_deadline(monkeypatch):
    seen = _users(monkeypatch)
    monkeypatch.setattr(tick.settings, "worker_tick_soft_deadline_s", 0.01)
    # UTC 19:45 = KST 04:45 — 이 시각의 마지막 틱. 상한이 soft+60초라 0.15초 걸리는 3명은 모두 처리된다.
    counts = await tick.run_tick(datetime(2026, 10, 5, 19, 45, tzinfo=timezone.utc))
    assert len(seen) == 3 and counts["deadline_skipped"] == 0


@pytest.mark.parametrize("hour,minute,skipped", [(22, 30, 2), (23, 15, 0)])
async def test_last_tick_is_judged_by_local_minute(monkeypatch, hour, minute, skipped):
    """30분 오프셋 tz(인도)는 UTC :30에 현지 04:00, UTC :15에 현지 04:45 — 현지 분으로 판정한다."""
    _users(monkeypatch, tz="Asia/Kolkata")
    monkeypatch.setattr(tick.settings, "worker_tick_soft_deadline_s", 0.01)
    counts = await tick.run_tick(datetime(2026, 10, 5, hour, minute, tzinfo=timezone.utc))
    assert counts["deadline_skipped"] == skipped


async def test_soft_deadline_zero_disables(monkeypatch):
    seen = _users(monkeypatch)
    monkeypatch.setattr(tick.settings, "worker_tick_soft_deadline_s", 0)
    counts = await tick.run_tick(datetime(2026, 10, 5, 19, 0, tzinfo=timezone.utc))
    assert len(seen) == 3 and counts["deadline_skipped"] == 0


async def test_default_soft_deadline_does_not_cut_a_normal_tick(monkeypatch):
    seen = _users(monkeypatch)
    counts = await tick.run_tick(datetime(2026, 10, 5, 19, 0, tzinfo=timezone.utc))
    assert len(seen) == 3 and counts["deadline_skipped"] == 0


def test_soft_deadline_fits_under_systemd_timeout():
    """데드라인 + 유저 1명 최악 + 기동·후처리 여유(40s)가 systemd TimeoutStartSec(14min) 안이어야 한다.

    마지막 틱 묶음의 상한(soft+60=720초)은 그 끝(soft+user_timeout=780초)보다 앞이어야 한다 —
    유저별 한도를 남은 시간으로 줄여 780초를 넘겨 돌지 않으므로 위 합이 그대로 최악이다."""
    s = tick.settings
    assert s.worker_tick_soft_deadline_s + s.worker_user_timeout_s + 40 < 14 * 60
    assert tick._LAST_TICK_EXTRA_S < s.worker_user_timeout_s
    assert s.worker_tick_soft_deadline_s + tick._LAST_TICK_EXTRA_S == 720


def _clocked(monkeypatch, counts_by_tz: dict[str, int], cost: float):
    """tz별 유저를 만들고 틱 시계만 가짜로 바꾼다(asyncio 시계는 그대로). 유저 1명 = cost초.

    반환: (tz별 프로필, 처리 순서, _profile_id_batches 호출 tz 묶음)."""
    profiles = {
        tz: [SimpleNamespace(id=uuid.uuid4(), timezone=tz) for _ in range(n)]
        for tz, n in counts_by_tz.items()
    }
    monkeypatch.setattr(
        tick, "get_sessionmaker",
        _fake_get_sessionmaker([p for ps in profiles.values() for p in ps]),
    )
    clock = [0.0]
    monkeypatch.setattr(tick, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    seen = []

    async def _work(now, pid, cfg, *, diary_policy=None):
        seen.append(pid)
        clock[0] += cost
        return {"diaries": 1}

    monkeypatch.setattr(tick, "_process_user", _work)
    batches = []
    real = tick._profile_id_batches

    def _spy(batch_size, tzs):
        batches.append(set(tzs))
        return real(batch_size, tzs)

    monkeypatch.setattr(tick, "_profile_id_batches", _spy)
    return profiles, seen, batches


async def test_last_tick_timezone_first_then_others_until_soft_deadline(monkeypatch):
    """KST 04:15: 다윈은 04:45(마지막 틱), 서울은 04:15. 예전엔 다윈 하나 때문에 틱 전체가 무제한이었다.

    다윈을 먼저 끝까지 처리하고, 서울은 틱 시작부터 660초까지만 처리한 뒤 나머지는 넘긴다."""
    profiles, seen, batches = _clocked(
        monkeypatch, {"Australia/Darwin": 2, "Asia/Seoul": 10}, cost=100,
    )
    counts = await tick.run_tick(datetime(2026, 10, 5, 19, 15, tzinfo=timezone.utc))
    darwin = {p.id for p in profiles["Australia/Darwin"]}
    assert set(seen[:2]) == darwin  # 다윈 전원을 먼저
    # 서울: 200·300·400·500·600초에 시작한 5명만, 700초부터는 넘김.
    assert len(seen) == 7 and counts["diaries"] == 7
    assert counts["deadline_skipped"] == 5 and counts["users"] == 12
    assert counts["timed_out"] == 0
    assert batches == [{"Australia/Darwin"}, {"Asia/Seoul"}]


async def test_last_tick_group_is_capped_at_720s(monkeypatch):
    """KST 04:45: 서울·도쿄가 직접 마지막 틱이면 그 묶음이 가장 크다 — 무제한이 아니라 720초까지만.

    같은 틱에 05:15인 다윈(이어 만들기)은 나머지 묶음이라 660초가 이미 지나 넘긴다."""
    profiles, seen, batches = _clocked(
        monkeypatch, {"Asia/Seoul": 10, "Australia/Darwin": 2}, cost=100,
    )
    counts = await tick.run_tick(datetime(2026, 10, 5, 19, 45, tzinfo=timezone.utc))
    seoul = {p.id for p in profiles["Asia/Seoul"]}
    # 0~700초에 시작한 8명(660초 상한이었다면 7명), 800초부터 넘김.
    assert len(seen) == 8 and set(seen) <= seoul
    assert counts["deadline_skipped"] == 2 + 2  # 서울 2 + 다윈 2
    assert batches == [{"Asia/Seoul"}, {"Australia/Darwin"}]


async def test_last_tick_user_is_cut_at_tick_end_not_counted_as_timeout(monkeypatch):
    """마지막 묶음에서 660초 뒤 시작한 유저는 soft+user_timeout을 넘겨 돌지 않는다.

    그 끝에서 멈춘 유저는 멈춘 LLM/DB가 아니므로 timed_out(경보)이 아니라 deadline_skipped로 센다."""
    monkeypatch.setattr(tick.settings, "worker_user_timeout_s", 60.05)  # 끝 = 660+60.05 = 720.05초
    profiles = [SimpleNamespace(id=uuid.uuid4(), timezone="Asia/Seoul") for _ in range(2)]
    monkeypatch.setattr(tick, "get_sessionmaker", _fake_get_sessionmaker(profiles))
    clock = [0.0]
    monkeypatch.setattr(tick, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = []

    async def _work(now, pid, cfg, *, diary_policy=None):
        calls.append(pid)
        if len(calls) == 1:
            clock[0] = 720.0  # 첫 유저가 720초까지 사용 → 둘째는 상한(720) 안에서 시작, 남은 0.05초
            return {"diaries": 1}
        await asyncio.sleep(0.5)  # 남은 시간보다 오래 걸림
        return {"diaries": 1}

    monkeypatch.setattr(tick, "_process_user", _work)
    counts = await tick.run_tick(datetime(2026, 10, 5, 19, 45, tzinfo=timezone.utc))
    assert len(calls) == 2
    assert counts["diaries"] == 1 and counts["deadline_skipped"] == 1
    assert counts["timed_out"] == 0


async def test_without_last_tick_timezone_behavior_is_unchanged(monkeypatch):
    """:45 시간대가 없으면 종전과 같다 — 한 묶음, 660초 상한, 유저 한도 그대로."""
    _, seen, batches = _clocked(monkeypatch, {"Asia/Seoul": 10}, cost=100)
    waits = []
    real_wait_for = asyncio.wait_for

    async def _wait_for(aw, timeout):
        waits.append(timeout)
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(tick.asyncio, "wait_for", _wait_for)
    counts = await tick.run_tick(datetime(2026, 10, 5, 19, 0, tzinfo=timezone.utc))  # KST 04:00
    assert len(seen) == 7 and counts["deadline_skipped"] == 3  # 0~600초 시작 7명
    assert batches == [{"Asia/Seoul"}]
    assert waits == [tick.settings.worker_user_timeout_s] * 7


def test_summary_line_for_deadline():
    counts = {"diary_failed": 0, "diaries": 1, "diary_llm": 0, "diary_preset": 1, "diary_none": 0,
              "morning": 0, "evening": 0, "users": 3, "deadline_skipped": 2}
    text = tick._build_summary(datetime(2026, 10, 5, 19, 0, tzinfo=timezone.utc), counts, 661.0)
    assert "⏱ 소프트 데드라인 도달 — 미처리 2명(다음 틱 인계)" in text
    assert not text.startswith("⚠️")  # 실패가 아니라 용량 신호
