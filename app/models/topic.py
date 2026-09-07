"""User-scoped banner offers and prepared chat openings; content is authored in Git."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey,
    ForeignKeyConstraint, Index, String, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class UserTopicState(Base):
    __tablename__ = "user_topic_states"
    __table_args__ = (
        UniqueConstraint("offer_id"),
        CheckConstraint("offer_sequence > 0", name="user_topic_sequence_positive"),
        CheckConstraint("placement = 'home_blind'", name="user_topic_placement"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True
    )
    placement: Mapped[str] = mapped_column(String, primary_key=True)
    offer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    offer_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    topic_id: Mapped[str] = mapped_column(String, nullable=False)
    topic_revision: Mapped[str] = mapped_column(String, nullable=False)
    questions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    day_high_watermark: Mapped[date] = mapped_column(Date, nullable=False)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    offered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ChatTopicEntry(Base):
    __tablename__ = "chat_topic_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "committed_message_id"], ["messages.user_id", "messages.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "first_user_message_id"], ["messages.user_id", "messages.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("state IN ('pending','committed','superseded')", name="topic_entry_state"),
        CheckConstraint("expires_at > created_at", name="topic_entry_expiration"),
        CheckConstraint(
            "state <> 'committed' OR "
            "(committed_message_id IS NOT NULL AND first_user_message_id IS NOT NULL)",
            name="topic_entry_committed_links",
        ),
        CheckConstraint(
            "state <> 'pending' OR "
            "(committed_message_id IS NULL AND first_user_message_id IS NULL)",
            name="topic_entry_pending_links",
        ),
        Index("topic_entry_one_pending", "user_id", unique=True,
              postgresql_where=text("state = 'pending'")),
        Index("topic_entry_one_answer", "user_id", "offer_id", unique=True,
              postgresql_where=text("first_user_message_id IS NOT NULL")),
        Index("topic_entry_offer_lookup", "user_id", "offer_id", "created_at"),
        Index("topic_entry_expiry", "expires_at", postgresql_where=text("state <> 'committed'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    placement: Mapped[str] = mapped_column(String, nullable=False)
    offer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    offer_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    topic_id: Mapped[str] = mapped_column(String, nullable=False)
    topic_revision: Mapped[str] = mapped_column(String, nullable=False)
    questions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    context_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'pending'"))
    timezone_name: Mapped[str] = mapped_column(String, nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    committed_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Also records a successful safety-first turn that omitted the prepared question.
    first_user_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

