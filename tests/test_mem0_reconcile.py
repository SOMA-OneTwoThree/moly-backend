"""고아 후보 정리 — crash 지점별 잔재를 수렴시킨다.

provider collection을 무제한 scan해 추측하지 않는다. durable planned 후보의 결정 UUID로만
확인한다 — 그게 우리가 만든 것의 유일한 신뢰 가능한 목록이다.
"""
from __future__ import annotations

import uuid

from app.services import mem0_reconcile as rc


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.executed: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params or {}))
        return _Res(self.rows)


async def test_stragglers_are_closed_when_registry_exists():
    """registry가 이미 있는데 planned로 남은 건 닫기만 실패한 것 — 삭제 대상이 아니다."""
    s = _Session(rows=[(uuid.uuid4(),), (uuid.uuid4(),)])
    assert await rc.close_stragglers(s) == 2
    sql = s.executed[0][0]
    assert "status='committed'" in sql
    assert "EXISTS" in sql and "mem0_memory_registry" in sql


async def test_orphan_query_excludes_rows_with_registry():
    s = _Session(rows=[])
    await rc.find_orphans(s)
    sql, params = s.executed[0]
    assert "NOT EXISTS" in sql and "mem0_memory_registry" in sql
    assert params["minutes"] == rc.ORPHAN_AFTER_MINUTES
    assert "LIMIT :limit" in sql  # bounded


async def test_orphan_query_ignores_recent_candidates():
    """진행 중인 잡을 고아로 오인하면 정상 처리를 죽인다."""
    s = _Session(rows=[])
    await rc.find_orphans(s)
    assert "created_at < now() - make_interval" in s.executed[0][0]


async def test_resolve_marks_dead_and_reports_vector_action():
    o = rc.OrphanCandidate(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), 3)
    s = _Session()
    assert await rc.resolve_orphan(s, o, exists_in_provider=True) == "deleted_vector"
    assert await rc.resolve_orphan(s, o, exists_in_provider=False) == "closed_only"
    assert "status='dead'" in s.executed[0][0]


def test_reconciler_never_scans_provider_collection():
    """무제한 scan으로 추측하면 다른 사용자 벡터까지 건드릴 수 있다."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(rc))
    called = {
        n.func.attr for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    for banned in ("list", "scan", "delete_by_user", "search"):
        assert banned not in called


async def test_catch_up_skips_users_with_pending_judgments():
    """판정 안 된 기억을 통과시키면 검색에 안 잡히는 구간이 생긴다."""
    s = _Session(rows=[])
    await rc.catch_up_consolidated(s)
    sql = s.executed[0][0]
    assert "semantic_status = 'pending'" in sql
    assert "NOT EXISTS" in sql
    assert "consolidated_through_turn_seq < s.ingest_through_turn_seq" in sql


async def test_catch_up_never_passes_ingest_cursor():
    """consolidation이 ingest를 앞지를 수 없다."""
    s = _Session(rows=[])
    await rc.catch_up_consolidated(s)
    assert "= s.ingest_through_turn_seq" in s.executed[0][0]
