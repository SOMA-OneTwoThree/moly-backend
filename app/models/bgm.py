"""Server-owned catalogue, independent of member state."""
from sqlalchemy import Boolean, Integer, String, BigInteger, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base


class BgmTrackRecord(Base):
    __tablename__ = "bgm_tracks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    category: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    title_i18n: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    source: Mapped[str] = mapped_column(String)
    revision: Mapped[str] = mapped_column(String)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
