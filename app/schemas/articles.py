from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from app.schemas.common import StrictResponse

class ArticleFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feedback: Literal["helpful", "not_helpful"]

class ArticleActivity(StrictResponse):
    article_id: str
    like_count: int = Field(ge=0)
    feedback: Literal["helpful", "not_helpful"] | None
    is_read: bool
    reward_claimed: bool
    granted: int = Field(ge=0)
    balance_after: int | None

class ArticleActivityList(StrictResponse):
    articles: list[ArticleActivity]
