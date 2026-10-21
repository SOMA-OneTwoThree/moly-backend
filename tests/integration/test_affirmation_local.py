"""Opt-in real SQL coverage for 오늘의 글귀 확인 기록; disposable local moly_schema_* DB only.

MOLY_SCHEMA_TEST_DSN=postgresql://postgres@localhost:54329/moly_schema_affirmation_it
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.app_day import AppDay
from app.core.errors import AppError
from app.models.user_daily_stats import UserDailyStats
from app.services import affirmation
from db.schema_contract import require_scratch

ZONE = "Asia/Seoul"
NOW = datetime(2026, 9, 9, 15, 30, tzinfo=timezone.utc)
DAY = AppDay.at(NOW, ZONE)


@pytest_asyncio.fixture
async def subject():
    dsn = os.environ.get("MOLY_SCHEMA_TEST_DSN")
    if not dsn:
        pytest.skip("MOLY_SCHEMA_TEST_DSN required")
    require_scratch(dsn)
    engine = create_async_engine(
        dsn.replace("postgresql://", "postgresql+asyncpg://"), poolclass=NullPool
    )
    uid = uuid.uuid4()
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            # 가입 트리거가 profiles·삭제 장벽을 만든다. 시간대는 헤더 생략 경로 검증용이다.
            await session.execute(
                text("INSERT INTO auth.users(id, created_at) VALUES(:id, :created)"),
                {"id": uid, "created": NOW},
            )
            await session.execute(
                text("UPDATE public.profiles SET timezone = :zone WHERE id = :id"),
                {"zone": ZONE, "id": uid},
            )
            await session.commit()
            try:
                yield session, uid
            finally:
                await session.rollback()
                await session.execute(
                    text("DELETE FROM auth.users WHERE id = :id"), {"id": uid}
                )
                await session.execute(
                    text("DELETE FROM public.privacy_subject_barriers WHERE user_id = :id"),
                    {"id": uid},
                )
                await session.commit()
    finally:
        await engine.dispose()


async def daily_row(session: AsyncSession, uid: uuid.UUID) -> UserDailyStats | None:
    rows = await session.execute(
        select(UserDailyStats).where(
            UserDailyStats.user_id == uid, UserDailyStats.activity_date == DAY.local_date
        )
    )
    row = rows.scalars().first()
    if row is not None:
        await session.refresh(row)
    return row


async def test_acknowledge_creates_the_daily_row_once_and_status_follows(subject):
    session, uid = subject
    assert await daily_row(session, uid) is None
    before = await affirmation.status(
        session, str(uid), locale="ko", timezone_name=ZONE, now_utc=NOW
    )
    assert before["acknowledged"] is False
    assert await daily_row(session, uid) is None  # 조회는 행을 만들지 않는다

    result = await affirmation.acknowledge(
        session, str(uid), local_date=DAY.local_date, timezone_name=ZONE, now_utc=NOW
    )
    assert result == {"local_date": DAY.local_date, "acknowledged": True}
    row = await daily_row(session, uid)
    assert row is not None and row.affirmation_acknowledged_at == NOW
    assert row.tokens_used == 0 and row.attendance_claimed_at is None

    again = await affirmation.acknowledge(
        session, str(uid), local_date=DAY.local_date, timezone_name=ZONE,
        now_utc=NOW + timedelta(hours=1),
    )
    assert again == result
    assert (await daily_row(session, uid)).affirmation_acknowledged_at == NOW
    assert await session.scalar(
        select(func.count()).select_from(UserDailyStats).where(UserDailyStats.user_id == uid)
    ) == 1

    # 시간대 헤더를 생략하면 저장된 profiles.timezone으로 같은 현지 날짜를 계산한다.
    after = await affirmation.status(
        session, str(uid), locale="ja", timezone_name=None, now_utc=NOW
    )
    assert after["acknowledged"] is True
    assert after["local_date"] == DAY.local_date
    assert after["affirmation"]["id"] == before["affirmation"]["id"]
    assert after["locale"] == "ja" and after["affirmation"]["text"] != before["affirmation"]["text"]


async def test_stale_local_date_is_rejected_without_creating_a_row(subject):
    session, uid = subject
    with pytest.raises(AppError) as error:
        await affirmation.acknowledge(
            session, str(uid), local_date=DAY.local_date - timedelta(days=1),
            timezone_name=ZONE, now_utc=NOW,
        )
    assert error.value.code == "DAILY_AFFIRMATION_STALE"
    assert error.value.http_status == 409
    await session.rollback()
    assert await daily_row(session, uid) is None
    unchanged = await affirmation.status(
        session, str(uid), locale="ko", timezone_name=ZONE, now_utc=NOW
    )
    assert unchanged["acknowledged"] is False
