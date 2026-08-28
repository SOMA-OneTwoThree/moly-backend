"""mem0 vector-store façade — `Memory`를 쓰지 않고 벡터 인덱스 계층만 감싼다.

기억 재설계 3단계 게이트(docs/ARCHITECTURE-capi.md 5.6절).

**왜 public API를 안 쓰나 (실측 근거, mem0ai==2.0.11)**

1. `Memory.add()`는 `infer` 값과 무관하게 SQLite history(`add_history`)를 친다. 호스트마다 다른
   history가 추출 결과를 바꾸므로 다중 worker에서 결과가 갈린다.
2. `mem0.vector_stores.supabase.Supabase.__init__`은 `vecs.create_client()`를 불러
   **호출자가 제어할 수 없는 SQLAlchemy 기본 풀(5+10)** 을 만들고, `list_cols()/create_col()`로
   **런타임 DDL**(schema/extension/table 생성)을 친다.
3. `vecs.Client.__init__`은 `create schema`·`create extension`을 실행한다 — 런타임 롤에 CREATE
   권한을 요구하게 된다.

그래서 이 모듈은 **벡터 인덱스 연산만** 직접 감싼다. `Memory`는 import 경로에 딸려올 뿐 인스턴스를
만들지 않으므로 history DB 파일이 생기지 않는다(계약 테스트로 고정).

**불변식**
 · `Memory`/`AsyncMemory`를 생성하지 않는다. history 파일·호출 0.
 · engine은 주입받는다. 런타임 DDL 0 — 스키마/컬렉션/인덱스는 migration 롤이 만든다.
 · 모든 연산은 bounded다(limit·timeout). vecs는 동기라 스레드에서 실행하고 timeout을 건다.
 · 결과는 쓰기 전에 `user_id`를 재검증한다. provider 필터를 최종 방어선으로 믿지 않는다.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger("moly-backend")

# 이 façade가 의존하는 upstream 심볼. 버전이 바뀌어 하나라도 사라지면 계약 테스트가 먼저 깨진다.
PINNED_MEM0_VERSION = "2.0.11"
REQUIRED_COLLECTION_METHODS = ("upsert", "fetch", "query", "delete")


class Mem0ContractError(RuntimeError):
    """upstream 구조가 가정과 다르다 — 조용히 우회하지 않고 실패시킨다."""


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """저장/조회 단위. 본문·임베딩의 정본은 여기가 아니라 우리 DB와 source다."""

    id: str
    embedding: list[float]
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SearchHit:
    id: str
    # ⚠️ **거리다. 유사도가 아니다.** vecs는 cosine distance를 주므로 **낮을수록 가깝다**.
    #    `score`라는 이름으로 두면 큰 값이 좋은 줄 알고 내림차순 정렬하게 되고, 그러면
    #    가장 관련 없는 기억이 프롬프트에 실린다(실측 사고). 이름으로 못 박는다.
    distance: float
    payload: dict[str, Any]


def build_sync_engine(dsn: str, *, pool_size: int = 3, max_overflow: int = 0):
    """벡터 store 전용 **동기** engine.

    ⚠️ 앱의 asyncpg 엔진을 쓸 수 없다. vecs는 동기 SQLAlchemy라 asyncpg 기반 엔진을 넘기면
    `MissingGreenlet`으로 터진다(실측). 그래서 psycopg2 동기 드라이버로 별도 엔진을 만든다.
    psycopg3는 서버측 바인딩이라 vecs의 가 문법 오류로
    터진다(실측) — vecs가 psycopg2를 전제로 쓰여 있다.

    풀은 명시 상한을 준다 — vecs 기본 클라이언트의 5+10 무제어 풀을 쓰지 않기 위한 것이
    이 façade의 존재 이유 중 하나다(9.1절).
    """
    from sqlalchemy import create_engine

    sync_dsn = dsn
    for prefix in ("postgresql+asyncpg://", "postgres://", "postgresql://"):
        if sync_dsn.startswith(prefix):
            sync_dsn = "postgresql+psycopg2://" + sync_dsn.split("://", 1)[1]
            break
    return create_engine(
        sync_dsn,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=2,
        pool_pre_ping=True,
    )


def build_client(engine) -> Any:
    """주입한 **동기** engine으로 `vecs.Client`를 만든다. **DDL을 치지 않는다.**

    `Client.__init__`이 create_engine + create schema/extension을 하므로 우회하고 필요한 속성만
    직접 채운다. 이 결합은 깨지기 쉬우므로 계약 테스트가 upstream 속성 이름을 고정한다.
    """
    import vecs
    from sqlalchemy import MetaData
    from sqlalchemy.orm import sessionmaker

    client = vecs.Client.__new__(vecs.Client)
    client.engine = engine
    client.meta = MetaData(schema="vecs")
    client.Session = sessionmaker(engine)
    # HNSW 지원 판정에만 쓰인다. 인덱스는 migration이 만들므로 조회하지 않고 고정한다.
    client.vector_version = "0.8.0"
    for attr in ("engine", "meta", "Session", "vector_version"):
        if not hasattr(client, attr):  # pragma: no cover - 방어
            raise Mem0ContractError(f"vecs.Client에 {attr} 속성이 없다")
    return client


class Mem0VectorIndexAdapter:
    """v2 컬렉션 하나에 대한 bounded CRUD. 모든 경로가 `user_id`로 격리된다."""

    def __init__(self, client: Any, collection_name: str, *, dimension: int = 1536) -> None:
        self._client = client
        self.collection_name = collection_name
        self.dimension = dimension
        self._collection: Any | None = None

    def _col(self) -> Any:
        """이미 존재하는 컬렉션만 연다. 없으면 만들지 않고 실패시킨다(런타임 DDL 금지)."""
        if self._collection is None:
            from vecs.collection import Collection

            col = Collection(self.collection_name, self.dimension, self._client)
            missing = [m for m in REQUIRED_COLLECTION_METHODS if not hasattr(col, m)]
            if missing:
                raise Mem0ContractError(f"vecs Collection에 없는 메서드: {missing}")
            self._collection = col
        return self._collection

    async def _run(self, fn, *args, timeout: float):
        """동기 vecs 호출을 스레드에서 실행하고 상한을 건다.

        vecs는 동기 SQLAlchemy라 이벤트 루프를 막는다. timeout을 못 걸면 lease보다 오래 붙잡혀
        fencing이 깨지므로 adapter 계약 실패로 본다(13.2절).
        """
        # ⚠️ **wait_for는 기다림만 끊는다. 스레드는 계속 돈다.** 파이썬은 스레드를 죽일 수
        # 없으므로, 타임아웃 뒤에도 그 DB 작업은 끝까지 실행된다. 우리가 얻는 건 "제때
        # 포기하고 lease를 지킨다"이지 "작업이 멈춘다"가 아니다.
        #
        # 그래서 이 계약이 성립하려면 **모든 쓰기가 멱등**이어야 한다. 뒤늦게 끝난 upsert가
        # 같은 결정적 id로 같은 값을 쓰기 때문에 안전하다(9.2절). 새 쓰기 경로를 추가할 때
        # 이 전제를 깨지 않도록 주의한다.
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=timeout)

    async def insert_many(
        self, records: list[VectorRecord], *, user_id: str, timeout: float = 12.0
    ) -> list[str]:
        """같은 id로 upsert한다 — crash retry가 같은 행으로 수렴한다(랜덤 중복 방지).

        서로 다른 candidate를 의미 기반으로 합치지 않는다. 그건 registry consolidation의 일이다.
        """
        if not records:
            return []
        for r in records:
            if r.payload.get("user_id") != user_id:
                raise Mem0ContractError("payload user_id가 인증 사용자와 다르다")
            if len(r.embedding) != self.dimension:
                raise Mem0ContractError(
                    f"임베딩 차원 불일치: {len(r.embedding)} != {self.dimension}"
                )
        rows = [(r.id, r.embedding, r.payload) for r in records]
        col = self._col()
        await self._run(col.upsert, rows, timeout=timeout)
        return [r.id for r in records]

    async def get_many(
        self, ids: list[str], *, user_id: str, timeout: float = 6.0
    ) -> list[VectorRecord]:
        """full identity로 최대 N건. 반환 전 payload의 user_id를 다시 확인한다."""
        if not ids:
            return []
        col = self._col()
        raw = await self._run(col.fetch, ids, timeout=timeout)
        out: list[VectorRecord] = []
        for row in raw or []:
            rid, vec, payload = _unpack(row)
            if (payload or {}).get("user_id") != user_id:
                _log.error("mem0 fetch에 타 사용자 결과가 섞였다 — id=%s (폐기)", rid)
                continue
            # ⚠️ `vec or []` 금지 — numpy 배열은 진리값이 모호해 ValueError로 터진다(실측).
            out.append(
                VectorRecord(
                    id=str(rid),
                    embedding=list(vec) if vec is not None else [],
                    payload=dict(payload) if payload else {},
                )
            )
        return out

    async def search(
        self,
        embedding: list[float],
        *,
        user_id: str,
        limit: int = 25,
        timeout: float = 5.0,
    ) -> list[SearchHit]:
        """user filter를 provider에 걸고, 받은 결과의 user_id를 **다시** 검증한다."""
        col = self._col()

        def _q():
            return col.query(
                data=embedding,
                limit=limit,
                filters={"user_id": {"$eq": user_id}},
                include_value=True,
                include_metadata=True,
            )

        raw = await self._run(_q, timeout=timeout)
        hits: list[SearchHit] = []
        for row in raw or []:
            rid, score, payload = _unpack_hit(row)
            if (payload or {}).get("user_id") != user_id:
                _log.error("mem0 search에 타 사용자 결과가 섞였다 — id=%s (폐기)", rid)
                continue
            hits.append(SearchHit(id=str(rid), distance=float(score or 0.0), payload=payload or {}))
        return hits

    async def delete(self, ids: list[str], *, timeout: float = 6.0) -> int:
        if not ids:
            return 0
        col = self._col()
        await self._run(col.delete, ids, timeout=timeout)
        return len(ids)

    async def delete_by_user(
        self, user_id: str, *, limit: int = 500, timeout: float = 10.0
    ) -> int:
        """계정 삭제의 1차 bounded continuation. 한 번에 전량을 지우지 않는다.

        public `Memory.delete_all()`은 쓰지 않는다(12.3절 6번). 남은 잔존은 호출측이 직접 SQL로
        확인·정리하고 두 sweep 0을 검증한다.

        #21: 열거를 영벡터 유사도 검색(전 벡터 거리 계산 = vecs Seq Scan)에서 직접 SQL로
        대체했다. `metadata->>'user_id'` 동등은 기존 moly_memories_v2_user_idx를 타고,
        user_id 스코프가 SQL 술어 자체라 provider 필터 재검증이 필요 없다(타 사용자 행은
        구조적으로 대상 밖). bounded 계약은 서브쿼리 LIMIT으로 유지한다.
        """
        from sqlalchemy import text as _text

        from app.core.db import get_sessionmaker

        delete_sql = _text("""
            DELETE FROM vecs.moly_memories_v2
            WHERE id IN (
              SELECT id FROM vecs.moly_memories_v2
              WHERE metadata->>'user_id' = :user_id
              LIMIT :limit
            )
        """)

        async def _del() -> int:
            async with get_sessionmaker()() as session:
                res = await session.execute(delete_sql, {"user_id": user_id, "limit": limit})
                await session.commit()
                return int(res.rowcount or 0)

        return await asyncio.wait_for(_del(), timeout=timeout)


def _as_sequence(row: Any) -> list | None:
    """행을 위치 인덱싱 가능한 시퀀스로. 아니면 None.

    ⚠️ SQLAlchemy 2.0의 `Row`는 튜플처럼 보이지만 **tuple 서브클래스가 아니다**.
    `isinstance(row, tuple)`로 거르면 실제 결과가 전부 튕긴다(실측).
    """
    if isinstance(row, (tuple, list)):
        return list(row)
    if hasattr(row, "__len__") and hasattr(row, "__getitem__"):
        try:
            return [row[i] for i in range(len(row))]
        except (TypeError, IndexError, KeyError):
            return None
    return None


def _unpack(row: Any) -> tuple[Any, Any, Any]:
    """vecs fetch 행 → (id, vector, metadata). 길이 변화에 방어적으로 대응한다."""
    seq = _as_sequence(row)
    if seq is not None:
        if len(seq) >= 3:
            return seq[0], seq[1], seq[2]
        if len(seq) == 2:
            return seq[0], None, seq[1]
    raise Mem0ContractError(f"해석할 수 없는 fetch 행: {row!r}")


def _unpack_hit(row: Any) -> tuple[Any, Any, Any]:
    seq = _as_sequence(row)
    if seq is not None:
        if len(seq) >= 3:
            return seq[0], seq[1], seq[2]
        if len(seq) == 2:
            return seq[0], seq[1], {}
    raise Mem0ContractError(f"해석할 수 없는 search 행: {row!r}")
