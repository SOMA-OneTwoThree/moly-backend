"""mem0 vector-store façade 계약 — 15장 3단계 게이트.

여기서 통과하지 못하면 다음 단계로 가지 않는다. 검증 대상:
 1. `Memory`를 만들지 않는다 → SQLite history 파일이 생기지 않는다
 2. upstream(mem0ai==2.0.11 / vecs) 구조 가정이 실제와 맞는다
 3. 런타임 DDL을 치지 않는다(engine 주입)
 4. user filter + 결과 재검증으로 타 사용자 결과가 새지 않는다
 5. 모든 연산에 timeout이 걸린다(동기 vecs가 lease를 넘겨 붙잡지 못하게)
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest
from types import SimpleNamespace

from app.services import mem0_adapter as ma

# mem0ai·vecs는 pyproject에 고정된 하드 의존성이다. importorskip으로 넘기면 의존성 누락을
# CI가 '스킵'으로 통과시켜 버린다(실제로 vecs 미선언 상태가 이렇게 숨어 있었다).

UID = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
DIM = 4


# ─────────────────────────────────────────────────────────────
# 1. upstream 구조 가정 (실제 패키지를 본다)
# ─────────────────────────────────────────────────────────────
def test_pinned_version_matches_installed():
    import mem0

    assert mem0.__version__ == ma.PINNED_MEM0_VERSION


def test_vector_store_layer_is_usable_without_memory_class():
    """`Memory` 없이 벡터 계층만 import·사용 가능해야 façade가 성립한다."""
    from mem0.vector_stores.supabase import Supabase

    assert hasattr(Supabase, "insert") and hasattr(Supabase, "search")


def test_vecs_client_attributes_we_bypass_init_for_still_exist():
    """engine 주입은 `Client.__init__` 우회에 의존한다 — 속성 이름이 바뀌면 여기서 깨진다."""
    import inspect

    import vecs

    src = inspect.getsource(vecs.Client.__init__)
    # 우리가 우회하는 두 가지: 기본 풀 생성과 런타임 DDL.
    assert "create_engine(" in src
    assert "create schema if not exists" in src
    for attr in ("engine", "meta", "Session", "vector_version"):
        assert attr in src, f"Client.__init__이 더 이상 {attr}를 세팅하지 않는다"


def test_collection_methods_we_depend_on_exist():
    import vecs
    from vecs.collection import Collection

    for m in ma.REQUIRED_COLLECTION_METHODS:
        assert hasattr(Collection, m), f"vecs Collection에 {m}이 없다"
    assert vecs is not None


def test_importing_facade_does_not_create_history_db(tmp_path, monkeypatch):
    """게이트 핵심 — history 파일이 생기면 다중 host 결과가 갈린다."""
    monkeypatch.setenv("MEM0_DIR", str(tmp_path / "mem0"))
    # 이미 import된 상태라도, 지금까지 history db가 만들어지지 않았음을 확인한다.
    created = [p.name for p in pathlib.Path(str(tmp_path)).rglob("*.db")]
    assert created == []
    assert "mem0.memory.main" in sys.modules  # import는 딸려온다(무해)
    # 그러나 Memory 인스턴스는 아무 데서도 만들지 않는다.
    assert not any(
        isinstance(v, type(None)) is False and type(v).__name__ in ("Memory", "AsyncMemory")
        for v in vars(ma).values()
    )


def test_facade_source_never_instantiates_memory():
    """정적 검사 — public add/search 경로로 새는 호출을 금지한다."""
    src = pathlib.Path(ma.__file__).read_text()
    for banned in ("Memory(", "AsyncMemory(", "Memory.from_config", ".add_history("):
        assert banned not in src, f"façade가 {banned}를 쓰고 있다"


# ─────────────────────────────────────────────────────────────
# 2. adapter 동작 — 가짜 collection으로 계약만 본다
# ─────────────────────────────────────────────────────────────
class _FakeCollection:
    def __init__(self, *, delay: float = 0.0):
        self.rows: dict[str, tuple[list[float], dict]] = {}
        self.delay = delay
        self.queries: list[dict] = []

    def _sleep(self):
        if self.delay:
            import time

            time.sleep(self.delay)

    def upsert(self, records):
        self._sleep()
        for rid, vec, payload in records:
            self.rows[rid] = (vec, payload)

    def fetch(self, ids):
        self._sleep()
        return [(i, self.rows[i][0], self.rows[i][1]) for i in ids if i in self.rows]

    def query(self, *, data, limit, filters, include_value, include_metadata):
        self._sleep()
        self.queries.append(filters)
        out = []
        for rid, (_vec, payload) in list(self.rows.items())[:limit]:
            out.append((rid, 0.9, payload))
        return out

    def delete(self, ids=None, filters=None):
        self._sleep()
        if filters is not None:
            uid = filters["user_id"]["$eq"]
            gone = [r for r, (_v, p) in self.rows.items() if p.get("user_id") == uid]
        else:
            gone = [r for r in (ids or []) if r in self.rows]
        for r in gone:
            self.rows.pop(r, None)
        return gone


def _adapter(col) -> ma.Mem0VectorIndexAdapter:
    a = ma.Mem0VectorIndexAdapter(client=None, collection_name="moly_memories_v2", dimension=DIM)
    a._collection = col
    return a


def _rec(rid, uid=UID):
    return ma.VectorRecord(id=rid, embedding=[0.1] * DIM, payload={"user_id": uid, "turn_seq": 1})


async def test_insert_then_get_roundtrip():
    col = _FakeCollection()
    a = _adapter(col)
    assert await a.insert_many([_rec("m1"), _rec("m2")], user_id=UID) == ["m1", "m2"]
    got = await a.get_many(["m1", "m2"], user_id=UID)
    assert {r.id for r in got} == {"m1", "m2"}


async def test_insert_rejects_payload_for_another_user():
    """payload가 인증 사용자와 다르면 provider로 보내지 않는다."""
    a = _adapter(_FakeCollection())
    with pytest.raises(ma.Mem0ContractError):
        await a.insert_many([_rec("m1", uid=OTHER)], user_id=UID)


async def test_insert_rejects_wrong_dimension():
    a = _adapter(_FakeCollection())
    bad = ma.VectorRecord(id="m1", embedding=[0.1] * (DIM + 1), payload={"user_id": UID})
    with pytest.raises(ma.Mem0ContractError):
        await a.insert_many([bad], user_id=UID)


async def test_search_applies_user_filter_at_provider():
    col = _FakeCollection()
    a = _adapter(col)
    await a.insert_many([_rec("m1")], user_id=UID)
    await a.search([0.1] * DIM, user_id=UID)
    assert col.queries[0] == {"user_id": {"$eq": UID}}


async def test_search_discards_foreign_rows_even_if_provider_leaks():
    """provider 필터를 최종 방어선으로 믿지 않는다 — 결과를 다시 검증한다."""
    col = _FakeCollection()
    col.rows["leak"] = ([0.1] * DIM, {"user_id": OTHER})
    a = _adapter(col)
    assert await a.search([0.1] * DIM, user_id=UID) == []


async def test_get_many_discards_foreign_rows():
    col = _FakeCollection()
    col.rows["leak"] = ([0.1] * DIM, {"user_id": OTHER})
    a = _adapter(col)
    assert await a.get_many(["leak"], user_id=UID) == []


async def test_delete_by_user_is_bounded_direct_sql(monkeypatch):
    """#21 이후: 열거 없이 직접 SQL 1문. user_id 동등이 술어 자체(타 사용자 구조적 배제),
    서브쿼리 LIMIT이 bounded continuation 계약을 유지한다."""
    import app.core.db as core_db

    captured: dict = {}

    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, stmt, params=None):
            captured["sql"] = str(stmt)
            captured["params"] = params
            return SimpleNamespace(rowcount=2)

        async def commit(self):
            captured["committed"] = True

    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: lambda: _S())
    a = _adapter(_FakeCollection())
    assert await a.delete_by_user(UID, limit=500) == 2
    assert captured["committed"]
    assert captured["params"] == {"user_id": UID, "limit": 500}
    sql = captured["sql"]
    assert "metadata->>'user_id' = :user_id" in sql  # 술어가 곧 스코프
    assert "LIMIT :limit" in sql  # bounded 계약
    assert "vecs.moly_memories_v2" in sql


async def test_every_operation_is_bounded_by_timeout():
    """동기 vecs 호출이 lease보다 오래 붙잡으면 fencing이 깨진다 — 반드시 끊긴다."""
    col = _FakeCollection(delay=0.5)
    a = _adapter(col)
    with pytest.raises(asyncio.TimeoutError):
        await a.insert_many([_rec("m1")], user_id=UID, timeout=0.01)
    with pytest.raises(asyncio.TimeoutError):
        await a.search([0.1] * DIM, user_id=UID, timeout=0.01)


async def test_empty_inputs_do_not_touch_provider():
    col = _FakeCollection()
    a = _adapter(col)
    assert await a.insert_many([], user_id=UID) == []
    assert await a.get_many([], user_id=UID) == []
    assert await a.delete([]) == 0
    assert col.queries == [] and col.rows == {}


# ─────────────────────────────────────────────────────────────
# 3. 실 드라이버·행 형식 회귀 (전부 dev 실측으로 발견된 것들)
# ─────────────────────────────────────────────────────────────
def test_sync_engine_uses_psycopg2_not_asyncpg():
    """앱의 asyncpg 엔진을 넘기면 vecs 동기 호출이 MissingGreenlet으로 터진다."""
    eng = ma.build_sync_engine("postgresql+asyncpg://u:p@h:5432/db")
    assert eng.dialect.driver == "psycopg2"
    assert eng.dialect.name == "postgresql"


def test_sync_engine_normalizes_every_dsn_form():
    for dsn in (
        "postgresql+asyncpg://u:p@h/db", "postgres://u:p@h/db", "postgresql://u:p@h/db",
    ):
        assert ma.build_sync_engine(dsn).dialect.driver == "psycopg2"


def test_sync_engine_has_bounded_pool():
    """vecs 기본 5+10 무제어 풀을 쓰지 않는 것이 이 façade의 존재 이유 중 하나다."""
    eng = ma.build_sync_engine("postgresql://u:p@h/db", pool_size=3, max_overflow=0)
    assert eng.pool.size() == 3
    assert eng.pool._max_overflow == 0


class _RowLike:
    """SQLAlchemy 2.0 Row 흉내 — 튜플처럼 보이지만 tuple 서브클래스가 아니다."""

    def __init__(self, *values):
        self._v = values

    def __len__(self):
        return len(self._v)

    def __getitem__(self, i):
        return self._v[i]


def test_row_objects_are_unpacked_even_though_not_tuples():
    """isinstance(row, tuple)로 거르면 실제 결과가 전부 튕긴다(실측)."""
    rid, vec, payload = ma._unpack(_RowLike("id1", [0.1], {"user_id": "u"}))
    assert rid == "id1" and payload == {"user_id": "u"}


def test_numpy_like_vector_does_not_break_truthiness():
    """`vec or []`는 numpy 배열에서 ValueError로 터진다 — None 비교여야 한다."""
    import numpy as np

    row = _RowLike("id1", np.array([0.1, 0.2, 0.3]), {"user_id": "u"})
    rid, vec, payload = ma._unpack(row)
    assert len(list(vec)) == 3
