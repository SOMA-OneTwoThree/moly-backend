"""Read a complete active catalogue in one statement; never return partial success."""
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.i18n import resolve
from app.models.bgm import BgmTrackRecord
from app.schemas.banners import DEVELOPMENT_ASSET_ORIGIN
from app.schemas.bgm import BgmTrack, BgmTracksResponse


def render_track(row: BgmTrackRecord, locale: str | None) -> BgmTrack:
    names = row.title_i18n or {}
    track = BgmTrack(
        id=row.id, category=row.category, title=names.get(resolve(locale)) or row.title,
        source=row.source, revision=row.revision, url=row.url, sha256=row.sha256,
        size_bytes=row.size_bytes, mime_type=row.mime_type, sort_order=row.sort_order,
    )
    if (settings.environment not in {"local", "development"} and track.url
            and track.url.startswith(DEVELOPMENT_ASSET_ORIGIN + "/")):
        raise ValueError("development BGM asset in production catalogue")
    return track


async def list_tracks(session: AsyncSession, locale: str | None) -> BgmTracksResponse:
    rows = (await session.scalars(select(BgmTrackRecord).where(
        BgmTrackRecord.is_active.is_(True)
    ).order_by(BgmTrackRecord.sort_order, BgmTrackRecord.id))).all()
    tracks = [render_track(row, locale) for row in rows]
    canonical = json.dumps([track.model_dump() for track in tracks], sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False).encode()
    return BgmTracksResponse(revision=hashlib.sha256(canonical).hexdigest(), tracks=tracks)
