"""Per-account article feedback and one-time reward receipt."""
from __future__ import annotations
import uuid
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base

class ArticleProgress(Base):
    __tablename__ = "article_progress"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True)
    article_id: Mapped[str] = mapped_column(String, primary_key=True)
    feedback: Mapped[str | None] = mapped_column(String, nullable=True)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rewarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
