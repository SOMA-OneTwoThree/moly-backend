"""shadow 진입 오케스트레이터.

사용자당 단일 transaction으로 historical upper를 고정하고 ready로 열며, 최초 잡을
**정확히 하나만** 만든다. 여러 개면 커서 순서가 깨진다(15장 7번).
"""
from __future__ import annotations

import importlib.util
import pathlib
import uuid

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "enter_shadow_cohort.py"


def _load():
    spec = importlib.util.spec_from_file_location("shadow_entry", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Session:
    async def execute(self, stmt, params=None):
        return _Row(())


class _Row:
    def __init__(self, v):
        self._v = v

    def one(self):
        return self._v


@pytest.fixture
def mod(monkeypatch):
    m = _load()
    return m


async def _run(mod, monkeypatch, *, upper=42, earliest=1):
    calls = {"ready": 0, "enqueued": []}

    async def _lock(session, uid):
        return 4

    monkeypatch.setattr(mod, "lock_enrollment_subject", _lock)

    async def _enter(session, uid, **k):
        assert k["privacy_epoch"] == 4
        return upper

    async def _next(session, uid, *, cursor):
        return earliest

    async def _ready(session, uid):
        calls["ready"] += 1
        return True

    async def _enq(session, uid, *, turn_seq, **k):
        assert k["privacy_epoch"] == 4
        calls["enqueued"].append(turn_seq)

    monkeypatch.setattr(mod.memory_pipeline, "enter_shadow", _enter)
    monkeypatch.setattr(mod.memory_pipeline, "next_ingest_turn", _next)
    monkeypatch.setattr(mod.memory_pipeline, "mark_bootstrap_ready", _ready)
    monkeypatch.setattr(mod.memory_pipeline, "enqueue_ingest", _enq)
    msg = await mod._one(_Session(), uuid.uuid4(), apply=True)
    return msg, calls


async def test_enqueues_exactly_one_earliest_job(mod, monkeypatch):
    """15장 7번: '정확히 그 earliest 잡 하나만'. 여러 개면 커서 순서가 깨진다."""
    msg, calls = await _run(mod, monkeypatch, earliest=7)
    assert calls["enqueued"] == [7]
    assert calls["ready"] == 1
    assert "turn=7" in msg


async def test_user_with_no_legacy_markers_is_allowed(mod, monkeypatch):
    """이관할 게 없던 사용자(legacy 0)는 막지 않는다 — 신규 가입자가 여기 걸리면 안 된다."""
    _, calls = await _run(mod, monkeypatch, earliest=1)
    assert calls["enqueued"] == [1]


async def test_no_source_turns_stays_collecting(mod, monkeypatch):
    """처리할 turn이 없으면 ready로 열지 않는다 — 빈 채로 열면 live turn이 앞질러 들어간다."""
    msg, calls = await _run(mod, monkeypatch, earliest=None)
    assert calls["ready"] == 0 and calls["enqueued"] == []
    assert "collecting" in msg
