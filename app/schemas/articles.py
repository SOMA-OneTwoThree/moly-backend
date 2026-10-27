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

class ArticleBlock(StrictResponse):
    kind: Literal["paragraph", "heading", "callout", "bullet"]
    text: str
    highlights: list[str]

class ArticleContent(StrictResponse):
    id: str
    title: str
    summary: str
    read_minutes: int = Field(gt=0)
    cover: str
    preview_block_count: int | None = Field(ge=0)
    blocks: list[ArticleBlock]

class ArticleContentList(StrictResponse):
    articles: list[ArticleContent]
