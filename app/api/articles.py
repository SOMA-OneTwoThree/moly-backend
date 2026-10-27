from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.db import get_session
from app.core.security import get_current_user
from app.schemas.articles import ArticleActivity, ArticleActivityList, ArticleContentList, ArticleFeedbackRequest
from app.services import articles

router = APIRouter(tags=["articles"])

@router.get("/articles", response_model=ArticleContentList)
async def list_articles(user_id: str = Depends(get_current_user), session: AsyncSession = Depends(get_session),
                        x_app_locale: str | None = Header(default=None, alias="X-App-Locale")):
    return await articles.list_articles(session, x_app_locale)

@router.get("/article-progress", response_model=ArticleActivityList)
async def list_article_activity(user_id: str = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await articles.list_activity(session, user_id)

@router.put("/articles/{article_id}/feedback", response_model=ArticleActivity)
async def set_article_feedback(article_id: str, body: ArticleFeedbackRequest, user_id: str = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await articles.record(session, user_id, article_id, feedback=body.feedback)

@router.delete("/articles/{article_id}/feedback", response_model=ArticleActivity)
async def clear_article_feedback(article_id: str, user_id: str = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await articles.record(session, user_id, article_id, clear_feedback=True)

@router.post("/articles/{article_id}/reward", response_model=ArticleActivity)
async def claim_article_reward(article_id: str, user_id: str = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await articles.record(session, user_id, article_id)
