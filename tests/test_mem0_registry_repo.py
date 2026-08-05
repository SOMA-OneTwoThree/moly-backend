"""registry 전이 — component 원자성과 stale 판정 차단.

일부만 반영하면 supersede된 기억은 닫혔는데 새 기억은 active가 아닌 상태가 생겨,
그 사이 검색에 아무것도 안 잡힌다.
"""
from __future__ import annotations

import uuid

import pytest

from app.services import mem0_registry_repo as repo
from app.services.mem0_consolidation import Transition

UID = uuid.uuid4()


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _Session:
    def __init__(self, *, revision=1, epoch=0, applied=True):
        self.revision = revision
        self.epoch = epoch
        self.applied = applied
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):
        s = str(stmt)
        self.calls.append((s, params or {}))
        if s == str(repo._CHECK_REVISION):
            return _Res([(self.revision, self.epoch)])
        if s == str(repo._APPLY):
            return _Res([(params["id"],)] if self.applied else [])
        if s == str(repo._PENDING) or s == str(repo._COMPARISON_POOL):
            return _Res([])
        raise AssertionError(f"모르는 문장: {s[:60]}")


def _t(status="active", **kw):
    return Transition(registry_id=uuid.uuid4(), semantic_status=status, **kw)


async def test_all_transitions_applied_in_one_pass():
    s = _Session()
    n = await repo.apply_transitions(
        s, UID, [_t(), _t("superseded"), _t("duplicate")],
        expected_revision=1, classification_version="v1",
    )
    assert n == 3


async def test_stale_revision_blocks_publish():
    """판정 중 사용자 상태가 바뀌면 낡은 결과다 — 반영하지 않는다."""
    s = _Session(revision=5)
    with pytest.raises(repo.StaleConsolidation):
        await repo.apply_transitions(
            s, UID, [_t()], expected_revision=1, classification_version="v1"
        )
    assert not any(c[0] == str(repo._APPLY) for c in s.calls)  # 한 건도 안 썼다


async def test_missing_pipeline_row_blocks_publish():
    class _NoRow(_Session):
        async def execute(self, stmt, params=None):
            if str(stmt) == str(repo._CHECK_REVISION):
                return _Res([])
            return await super().execute(stmt, params)

    with pytest.raises(repo.StaleConsolidation):
        await repo.apply_transitions(
            _NoRow(), UID, [_t()], expected_revision=1, classification_version="v1"
        )


def test_comparison_pool_excludes_other_users_and_future_turns():
    sql = str(repo._COMPARISON_POOL)
    assert "r.user_id = :user_id" in sql
    assert "r.source_turn_seq < :turn_seq" in sql        # 자기 자신·미래 turn 제외
    assert "semantic_status IN ('active','ambiguous')" in sql
    assert "LIMIT :limit" in sql                          # bounded


def test_pending_query_is_scoped_to_turn_and_user():
    sql = str(repo._PENDING)
    assert "r.user_id = :user_id" in sql and "r.source_turn_seq = :turn_seq" in sql
    assert "semantic_status = 'pending'" in sql


def test_apply_never_revives_terminal_rows():
    """duplicate/superseded/excluded로 닫힌 행을 되살리지 않는다."""
    sql = str(repo._APPLY)
    assert "semantic_status IN ('pending','active','ambiguous')" in sql
    assert "revision = revision + 1" in sql
