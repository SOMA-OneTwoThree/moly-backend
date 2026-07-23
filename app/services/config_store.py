"""app_config 서버 설정 접근 — 서버 판정용 값(한도·임계) 조회·기록. 클라 노출과 분리."""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_config import AppConfig


async def get_config_values(session: AsyncSession, keys: list[str]) -> dict[str, Any]:
    """여러 key의 value(jsonb)를 dict로. 없는 key는 결과에서 빠짐(호출측이 기본값 처리)."""
    rows = await session.execute(select(AppConfig).where(AppConfig.key.in_(keys)))
    return {row.key: row.value for row in rows.scalars()}


async def set_config_value(session: AsyncSession, key: str, value: Any) -> None:
    """key 하나를 upsert(있으면 갱신). 모니터링 상태 기록용(예: 'monitoring:worker_last_success').

    키별 별도 row라 형제 키 클로버링 없음. 한도설정 키와 섞이지 않게 'monitoring:' 프리픽스 권장.
    저빈도 쓰기(워커 틱당 1회) 전제 — 핫리드(effective_token_config) 경합 무시가능.
    """
    stmt = pg_insert(AppConfig).values(key=key, value=value).on_conflict_do_update(
        index_elements=["key"], set_={"value": value, "updated_at": func.now()}
    )
    await session.execute(stmt)
    await session.commit()
