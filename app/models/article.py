"""Server-owned articles, plus per-account feedback and one-time reward receipt."""
from __future__ import annotations
import uuid
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base

class Article(Base):
    __tablename__ = "articles"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    cover: Mapped[str] = mapped_column(String)
    read_minutes: Mapped[int] = mapped_column(Integer)
    preview_block_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_i18n: Mapped[dict] = mapped_column(JSONB)

class ArticleProgress(Base):
    __tablename__ = "article_progress"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True)
    article_id: Mapped[str] = mapped_column(String, primary_key=True)
    feedback: Mapped[str | None] = mapped_column(String, nullable=True)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rewarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
