"""Complete BGM catalogue snapshots. A failed response is never a deletion signal."""
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.banners import DEVELOPMENT_ASSET_ORIGIN, PRODUCTION_ASSET_ORIGIN


class BgmTrack(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    category: Literal["lofi", "white_noise"]
    title: str = Field(min_length=1, max_length=120)
    source: Literal["bundled", "remote"]
    revision: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
    url: str | None
    sha256: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int | None = Field(gt=0, le=100 * 1024 * 1024)
    mime_type: Literal["audio/mp4", "audio/mpeg", "audio/wav"] | None
    sort_order: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_source(self):
        metadata = (self.url, self.sha256, self.size_bytes, self.mime_type)
        if self.source == "bundled":
            from app.services.banner_music import MUSIC_TRACKS
            if self.id not in {track[0] for track in MUSIC_TRACKS} or self.category != "lofi":
                raise ValueError("unknown bundled track")
            if any(value is not None for value in metadata):
                raise ValueError("bundled tracks have no remote metadata")
        else:
            if any(value is None for value in metadata):
                raise ValueError("remote metadata is required")
            parts = urlsplit(self.url)
            if (f"{parts.scheme}://{parts.netloc}" not in
                    {DEVELOPMENT_ASSET_ORIGIN, PRODUCTION_ASSET_ORIGIN}
                    or parts.query or parts.fragment
                    or not parts.path.startswith("/storage/v1/object/public/bgm-assets/")
                    or any(part in {".", ".."} for part in parts.path.split("/"))):
                raise ValueError("audio must use an immutable public BGM asset URL")
        return self


class BgmTracksResponse(BaseModel):
    schema_version: Literal[1] = 1
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    tracks: list[BgmTrack]
