"""유저 단위 트랜잭션 advisory lock — 재화·토큰·보상 경로의 동시 요청 직렬화 단일 구현.

키 표현(`hashtextextended(str(uid), 0)`)을 한 곳에 두어 chat·economy·ads가 **같은 직렬화
도메인**을 쓰게 한다. 복제 구현에서 키 표현이 갈라지면 서로 다른 락이 되어 직렬화가 조용히
깨진다(예: 보상 커서 조회와 지급이 서로를 못 막음). 트랜잭션 스코프라 커밋/롤백 시 자동
해제되고 pgbouncer(트랜잭션 풀링)와도 호환된다.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def advisory_xact_lock(session: AsyncSession, uid: uuid.UUID) -> None:
    """이 트랜잭션 동안 uid에 대한 배타 advisory lock 획득(커밋/롤백 시 자동 해제)."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:u, 0))"), {"u": str(uid)}
    )
