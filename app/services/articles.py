"""Active articles come from the DB. Feedback marks one read; rewards are atomic and once per user/article."""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.core import errors
from app.core.advisory_lock import advisory_xact_lock
from app.models.article import Article, ArticleProgress
from app.models.profile import Profile
from app.services import gating, hay_ledger, privacy
from app.services.i18n import resolve

REWARD_HAY = 20
_ACTIVE = select(Article).where(Article.is_active.is_(True)).order_by(Article.sort_order, Article.id)

def _view(article_id, row, granted=0, balance=None, like_count=0):
    return dict(article_id=article_id, feedback=row.feedback if row else None,
                is_read=row is not None, reward_claimed=bool(row and row.rewarded_at),
                granted=granted, balance_after=balance, like_count=like_count)

def _render(row: Article, locale: str | None):
    # 원고는 한국어가 원본이다. 번역이 없는 언어는 한국어 그대로 보여 준다.
    content = row.content_i18n.get(resolve(locale)) or row.content_i18n["ko"]
    return dict(id=row.id, title=content["title"], summary=content["summary"], read_minutes=row.read_minutes,
                cover=row.cover, preview_block_count=row.preview_block_count,
                blocks=[dict(kind=b["kind"], text=b["text"], highlights=b.get("highlights", [])) for b in content["blocks"]])

async def list_articles(session: AsyncSession, locale: str | None):
    return {"articles": [_render(row, locale) for row in (await session.scalars(_ACTIVE)).all()]}

async def list_activity(session: AsyncSession, user_id: str):
    ids = (await session.scalars(_ACTIVE.with_only_columns(Article.id))).all()
    rows = (await session.scalars(select(ArticleProgress).where(ArticleProgress.user_id == uuid.UUID(user_id)))).all()
    by_id = {row.article_id: row for row in rows}
    counts = dict((await session.execute(
        select(ArticleProgress.article_id, func.count())
        .where(ArticleProgress.feedback == "helpful", ArticleProgress.article_id.in_(ids))
        .group_by(ArticleProgress.article_id)
    )).all())
    return {"articles": [_view(id, by_id.get(id), like_count=counts.get(id, 0)) for id in ids]}

async def record(session: AsyncSession, user_id: str, article_id: str, *, feedback: str | None = None, clear_feedback: bool = False):
    uid = uuid.UUID(user_id)
    # Shared with wallet operations. Read current article access after waiting for the lock.
    await advisory_xact_lock(session, uid)
    article = (await session.execute(select(Article.id, Article.preview_block_count).where(
        Article.id == article_id, Article.is_active.is_(True)))).one_or_none()
    if article is None:
        raise errors.AppError("NOT_FOUND", 404, "아티클을 찾을 수 없어요.")
    await privacy.ensure_subject_active(session, uid)
    row = await session.get(ArticleProgress, (uid, article_id))
    now = datetime.now(timezone.utc)
    # Check only a new reward, before adding a read row that could autoflush.
    # A zero preview is still premium; existing reward receipts survive expiry.
    if (not clear_feedback and feedback is None
            and (row is None or row.rewarded_at is None)
            and article.preview_block_count is not None):
        entitlement = await gating.resolve_entitlement(session, str(uid), now)
        if entitlement["plan"] == "free":
            raise errors.AppError("ARTICLE_SUBSCRIPTION_REQUIRED", 403, "이 글의 보상은 구독 중에 받을 수 있어요.")
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
