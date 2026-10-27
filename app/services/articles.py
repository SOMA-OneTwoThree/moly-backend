"""Feedback marks an article read; rewards are atomic and once per user/article."""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.core import errors
from app.core.advisory_lock import advisory_xact_lock
from app.models.article import ArticleProgress
from app.models.profile import Profile
from app.services import hay_ledger

ARTICLE_IDS = tuple(f"sample-{i}" for i in range(1, 8))
REWARD_HAY = 20

def _view(article_id, row, granted=0, balance=None, like_count=0):
    return dict(article_id=article_id, feedback=row.feedback if row else None,
                is_read=row is not None, reward_claimed=bool(row and row.rewarded_at),
                granted=granted, balance_after=balance, like_count=like_count)

async def list_activity(session: AsyncSession, user_id: str):
    rows = (await session.scalars(select(ArticleProgress).where(ArticleProgress.user_id == uuid.UUID(user_id)))).all()
    by_id = {row.article_id: row for row in rows}
    counts = dict((await session.execute(
        select(ArticleProgress.article_id, func.count())
        .where(ArticleProgress.feedback == "helpful", ArticleProgress.article_id.in_(ARTICLE_IDS))
        .group_by(ArticleProgress.article_id)
    )).all())
    return {"articles": [_view(id, by_id.get(id), like_count=counts.get(id, 0)) for id in ARTICLE_IDS]}

async def record(session: AsyncSession, user_id: str, article_id: str, *, feedback: str | None = None, clear_feedback: bool = False):
    if article_id not in ARTICLE_IDS:
        raise errors.AppError("NOT_FOUND", 404, "아티클을 찾을 수 없어요.")
    uid = uuid.UUID(user_id)
    # Shared with the other wallet operations. Covers missing-row creation too.
    await advisory_xact_lock(session, uid)
    row = await session.get(ArticleProgress, (uid, article_id))
    now = datetime.now(timezone.utc)
    if row is None:
        row = ArticleProgress(user_id=uid, article_id=article_id, read_at=now)
        session.add(row)
    granted = 0
    balance = None
    if clear_feedback:
        # 평가 취소. 읽음과 보상 기록은 그대로 둔다.
        row.feedback = None
    elif feedback is not None:
        row.feedback = feedback
    elif row.rewarded_at is None:
        tx = await hay_ledger.apply(session, uid, "article_reward", REWARD_HAY)
        row.rewarded_at = now
        granted, balance = REWARD_HAY, tx.balance_after
    else:
        balance = await session.scalar(select(Profile.hay_balance).where(Profile.id == uid))
    await session.flush()
    like_count = await session.scalar(select(func.count()).select_from(ArticleProgress).where(
        ArticleProgress.article_id == article_id, ArticleProgress.feedback == "helpful"))
    result = _view(article_id, row, granted, balance, like_count=like_count)
    await session.commit()
    return result
