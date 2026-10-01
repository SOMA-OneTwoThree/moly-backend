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


async def test_soft_deadline_skips_rest_and_post_loop_still_runs(monkeypatch):
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
    summary = tick.slack_notify.send_summary.await_args.args[0]
    assert "소프트 데드라인 도달 — 미처리 2명(다음 틱 인계)" in summary
    # D6: 상태 채널(요약)에만 남긴다 — 경보 채널·데드맨 /fail로 올리지 않는다.
    tick.slack_notify.alert.assert_not_awaited()
    assert _GetClient.urls == ["https://hc.example/PING"]


async def test_soft_deadline_alone_still_sends_summary(monkeypatch):
    """처리 건수가 0(전원 미발행 등)이어도 데드라인 도달은 요약으로 남는다."""
    _users(monkeypatch, result={"diary_none": 1})
    monkeypatch.setattr(tick.settings, "worker_tick_soft_deadline_s", 0.01)
    counts = await tick.run_tick(datetime(2026, 10, 5, 19, 0, tzinfo=timezone.utc))
    assert counts["diaries"] == 0 and counts["deadline_skipped"] == 2
    tick.slack_notify.send_summary.assert_awaited_once()


async def test_soft_deadline_not_applied_on_last_tick_of_the_hour(monkeypatch):
    seen = _users(monkeypatch)
    monkeypatch.setattr(tick.settings, "worker_tick_soft_deadline_s", 0.01)
    # UTC 19:45 = KST 04:45 — 이 시각의 마지막 틱. 05:00 틱은 일기를 만들지 않으므로 끝까지 처리한다.
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
    """데드라인 + 유저 1명 최악 + 기동·후처리 여유(40s)가 systemd TimeoutStartSec(14min) 안이어야 한다."""
    s = tick.settings
    assert s.worker_tick_soft_deadline_s + s.worker_user_timeout_s + 40 < 14 * 60


def test_summary_line_for_deadline():
    counts = {"diary_failed": 0, "diaries": 1, "diary_llm": 0, "diary_preset": 1, "diary_none": 0,
              "morning": 0, "evening": 0, "users": 3, "deadline_skipped": 2}
    text = tick._build_summary(datetime(2026, 10, 5, 19, 0, tzinfo=timezone.utc), counts, 661.0)
    assert "⏱ 소프트 데드라인 도달 — 미처리 2명(다음 틱 인계)" in text
    assert not text.startswith("⚠️")  # 실패가 아니라 용량 신호
