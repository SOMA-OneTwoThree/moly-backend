"""shadow 진입 오케스트레이터 — 문서가 요구하는 **순서**를 코드가 강제하는지.

15장 7번은 tombstone 이관을 manifest보다 먼저 끝내라고 못박는다. 순서가 뒤집히면 지워달라던
내용이 v2 기억으로 되살아나고, 그건 cutover 즉시 중단 사유다. 그 순서는 주석이 아니라
코드가 지켜야 한다.
"""
from __future__ import annotations

import importlib.util
import pathlib
import uuid

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "enter_shadow_cohort.py"


def _load():
    """스크립트는 import 시 argv를 파싱하고 SystemExit을 낸다 — 함수만 떼어 쓴다."""
    src = _PATH.read_text().split("_env, _rest = split_env_arg")[0]
    spec = importlib.util.spec_from_loader("shadow_entry", loader=None)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(src, str(_PATH), "exec"), mod.__dict__)
    return mod


class _Session:
    """(legacy 마커 수, tombstone 수)를 흉내낸다."""

    def __init__(self, legacy: int, migrated: int):
        self._counts = (legacy, migrated)

    async def execute(self, stmt, params=None):
        return _Row(self._counts)


class _Row:
    def __init__(self, v):
        self._v = v

    def one(self):
        return self._v


@pytest.fixture
def mod(monkeypatch):
    m = _load()
    return m


async def _run(mod, monkeypatch, *, legacy, migrated, upper=42, earliest=1):
    calls = {"ready": 0, "enqueued": []}

    async def _enter(session, uid, **k):
        return upper

    async def _next(session, uid, *, cursor):
        return earliest

    async def _ready(session, uid):
        calls["ready"] += 1
        return True

    async def _enq(session, uid, *, turn_seq, **k):
        calls["enqueued"].append(turn_seq)

    monkeypatch.setattr(mod.memory_pipeline, "enter_shadow", _enter)
    monkeypatch.setattr(mod.memory_pipeline, "next_ingest_turn", _next)
    monkeypatch.setattr(mod.memory_pipeline, "mark_bootstrap_ready", _ready)
    monkeypatch.setattr(mod.memory_pipeline, "enqueue_ingest", _enq)
    msg = await mod._one(_Session(legacy, migrated), uuid.uuid4(), apply=True)
    return msg, calls


async def test_refuses_ready_when_tombstones_not_migrated(mod, monkeypatch):
    """이관 안 된 사용자를 ready로 열면 지워달라던 내용이 되살아난다."""
    with pytest.raises(RuntimeError) as e:
        await _run(mod, monkeypatch, legacy=5, migrated=0)
    assert "tombstone" in str(e.value)


async def test_no_job_is_enqueued_when_it_refuses(mod, monkeypatch):
    """거부하면서 잡만 흘리면 ready가 아닌 채로 색인이 돈다."""
    calls = {}
    with pytest.raises(RuntimeError):
        _, calls = await _run(mod, monkeypatch, legacy=5, migrated=0)
    assert calls.get("enqueued", []) == []


async def test_enqueues_exactly_one_earliest_job(mod, monkeypatch):
    """15장 7번: '정확히 그 earliest 잡 하나만'. 여러 개면 커서 순서가 깨진다."""
    msg, calls = await _run(mod, monkeypatch, legacy=3, migrated=3, earliest=7)
    assert calls["enqueued"] == [7]
    assert calls["ready"] == 1
    assert "turn=7" in msg


async def test_user_with_no_legacy_markers_is_allowed(mod, monkeypatch):
    """이관할 게 없던 사용자(legacy 0)는 막지 않는다 — 신규 가입자가 여기 걸리면 안 된다."""
    _, calls = await _run(mod, monkeypatch, legacy=0, migrated=0, earliest=1)
    assert calls["enqueued"] == [1]


async def test_no_source_turns_stays_collecting(mod, monkeypatch):
    """처리할 turn이 없으면 ready로 열지 않는다 — 빈 채로 열면 live turn이 앞질러 들어간다."""
    msg, calls = await _run(mod, monkeypatch, legacy=0, migrated=0, earliest=None)
    assert calls["ready"] == 0 and calls["enqueued"] == []
    assert "collecting" in msg
