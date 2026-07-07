"""DB 접근 계층 — SQLAlchemy 2.0 async. 테이블(DDL)은 팀원 소유(ERD/SOMA-152).

우리는 **이미 존재하는 스키마에 매핑되는 모델**로만 접근한다 — 마이그레이션/DDL 실행 안 함.
클라 직접 쓰기 없음(ERD §8) → 서버가 서비스 롤 연결로 읽고 쓴다.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """전 모델 공통 베이스. 모델은 각 모듈이 ERD를 거울처럼 반영해 정의."""


def _async_dsn() -> str:
    """Supabase 연결 문자열 → asyncpg 드라이버 DSN으로 정규화."""
    dsn = settings.supabase_db_connection_string
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql+asyncpg://", 1)
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return dsn


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """지연 생성 엔진(싱글턴). Supabase 풀러(pgbouncer) 호환 위해 prepared statement 캐시 비활성."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            _async_dsn(),
            pool_pre_ping=True,
            # pgbouncer(트랜잭션 풀링) 환경에서 asyncpg prepared statement 충돌 방지.
            connect_args={"statement_cache_size": 0},
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 의존성 — 요청 스코프 세션. 커밋/롤백은 서비스가 트랜잭션 경계로 관리."""
    async with get_sessionmaker()() as session:
        yield session
