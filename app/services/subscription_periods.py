"""Store access periods at a completed activity-day boundary."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.revenuecat_event import RevenuecatEvent
from app.models.subscription import Subscription


def period_time(value) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def contains(event: dict, at: datetime, owner_since: datetime | None = None) -> bool:
    start = period_time(event.get("purchased_at_ms"))
    end = period_time(event.get("expiration_at_ms"))
    return bool(start and end and start <= at < end and (owner_since is None or owner_since <= at))


async def period_events(session: AsyncSession, sub: Subscription) -> list[dict]:
    payload = RevenuecatEvent.payload
    query = select(payload).where(
        RevenuecatEvent.status == "processed",
        payload["type"].astext.in_(("INITIAL_PURCHASE", "RENEWAL")),
        payload["original_transaction_id"].astext == sub.original_transaction_id,
        payload["environment"].astext == sub.environment,
    )
    # A reconciled transfer retains the store chain and bounds its new owner.
    if sub.ownership_changed_at is None:
        query = query.where(payload["app_user_id"].astext == str(sub.user_id))
    return list((await session.execute(query)).scalars().all())


async def subscription_at(session: AsyncSession, user_id, at: datetime) -> Subscription | None:
    rows = (await session.execute(select(Subscription).where(
        Subscription.user_id == user_id,
        Subscription.status.in_(("active", "grace_period", "expired")),
        Subscription.expires_at > at,
        or_(Subscription.ownership_changed_at.is_(None), Subscription.ownership_changed_at <= at),
    ).order_by(Subscription.expires_at.desc()))).scalars().all()
    unresolved = False
    for sub in rows:
        start = sub.purchased_at
        if start is not None and start <= at:
            return sub
        events = await period_events(session, sub)
        if any(contains(event, at, sub.ownership_changed_at) for event in events):
            return sub
        latest_starts = set()
        for event in events:
            if event.get("transaction_id") != sub.latest_transaction_id:
                continue
            candidate = period_time(event.get("purchased_at_ms"))
            end = period_time(event.get("expiration_at_ms"))
            if candidate is not None and end is not None and candidate < end <= sub.expires_at:
                latest_starts.add(candidate)
        if len(latest_starts) == 1:
            if next(iter(latest_starts)) <= at:
                return sub
            continue
        # A known later period can legitimately follow an interval without access.
        # Missing period information remains retryable in the existing worker path.
        unresolved |= start is None
    if unresolved:
        raise ValueError("subscription period unavailable")
    return None
