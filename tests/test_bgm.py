import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import ValidationError

from app.schemas.bgm import BgmTrack
from app.services.bgm import list_tracks, render_track
from scripts.validate_bgm_publication import verify


def remote(**changes):
    raw = dict(id="rain", category="white_noise", title="Rain", source="remote",
               revision="v1", url="https://wywzjslvxwttxkecbyis.supabase.co/storage/v1/object/public/bgm-assets/rain/v1.m4a",
               sha256=hashlib.sha256(b"audio").hexdigest(), size_bytes=5,
               mime_type="audio/mp4", sort_order=6)
    return raw | changes


def test_source_contract():
    assert BgmTrack(**remote()).source == "remote"
    for changes in ({"sha256": None}, {"size_bytes": 0}, {"url": "https://evil.example/a"},
                    {"url": remote()["url"] + "?token=secret"}, {"source": "bundled"}):
        with pytest.raises(ValidationError):
            BgmTrack(**remote(**changes))


@pytest.mark.asyncio
async def test_full_snapshot_revision_locale_and_failure(monkeypatch):
    monkeypatch.setattr("app.services.bgm.settings.environment", "local")
    row = SimpleNamespace(**remote(), title_i18n={"ko": "빗소리"})
    result = MagicMock()
    result.all.return_value = [row]
    session = AsyncMock()
    session.scalars.return_value = result
    ko = await list_tracks(session, "ko")
    en = await list_tracks(session, "en")
    assert ko.tracks[0].title == "빗소리"
    assert ko.revision != en.revision
    assert ko.tracks[0].sha256 == en.tracks[0].sha256
    row.sha256 = None
    with pytest.raises(ValidationError):
        await list_tracks(session, "en")
    session.scalars.side_effect = RuntimeError("DB unavailable")
    with pytest.raises(RuntimeError):
        await list_tracks(session, "en")


def test_production_rejects_dev_assets(monkeypatch):
    monkeypatch.setattr("app.services.bgm.settings.environment", "production")
    with pytest.raises(ValueError, match="development BGM"):
        render_track(SimpleNamespace(**remote(), title_i18n=None), "en")


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,mime,valid", [
    (200, b"audio", "audio/mp4", True),
    (302, b"audio", "audio/mp4", False),
    (200, b"other", "audio/mp4", False),
    (200, b"audio!", "audio/mp4", False),
    (200, b"audio", "text/html", False),
])
async def test_publication_gate(status, body, mime, valid):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(status, content=body, headers={"content-type": mime})
    )) as client:
        if valid:
            await verify(BgmTrack(**remote()), client=client)
        else:
            with pytest.raises(ValueError):
                await verify(BgmTrack(**remote()), client=client)

@pytest.mark.asyncio
async def test_banner_remote_capability_and_failure_isolation(monkeypatch):
    from datetime import datetime, timezone
    from app.schemas.bgm import BgmTracksResponse
    from app.services.banner_catalog import capabilities
    from app.services.banners import list_banners
    from tests.test_banner_music import music_test_catalog
    catalog = music_test_catalog()
    caps = frozenset().union(*(capabilities(c) for b in catalog.manifest.banners
                              for c in b.canvases_by_locale.values())) - {"open_topic_conversation_v1"}
    snapshot = BgmTracksResponse(revision="a" * 64, tracks=[BgmTrack(**remote())])
    loader = AsyncMock(return_value=snapshot)
    monkeypatch.setattr("app.services.bgm.list_tracks", loader)
    session = AsyncMock()
    session.begin_nested = MagicMock(return_value=AsyncMock())
    kwargs = dict(now=datetime(2026, 9, 22, tzinfo=timezone.utc), platform="ios",
                  app_version="1.0.0", locale="en", timezone_name="UTC")
    await list_banners(catalog, session, "00000000-0000-0000-0000-000000000001",
                       capabilities=caps, **kwargs)
    loader.assert_not_called()
    feed = await list_banners(catalog, session, "00000000-0000-0000-0000-000000000001",
                             capabilities=caps | {"remote_bgm_v1"}, **kwargs)
    music = next(card for card in feed.items if card.id == "music-daily")
    assert next(e.text for e in music.canvas.elements if e.id == "message") == "Rain"
    loader.return_value = BgmTracksResponse(revision="b" * 64, tracks=[])
    feed = await list_banners(catalog, session, "00000000-0000-0000-0000-000000000001",
                             capabilities=caps | {"remote_bgm_v1"}, **kwargs)
    assert "music-daily" not in {card.id for card in feed.items}
    assert "mood-daily" in {card.id for card in feed.items}


def test_api_auth_locale_empty_snapshot(monkeypatch):
    from fastapi.testclient import TestClient
    from app.core.db import get_session
    from app.core.security import get_current_user
    from app.main import app
    from app.schemas.bgm import BgmTracksResponse
    loader = AsyncMock(return_value=BgmTracksResponse(revision="a" * 64, tracks=[]))
    monkeypatch.setattr("app.api.bgm.list_tracks", loader)
    async def session():
        yield "test-session"
    previous = dict(app.dependency_overrides)
    try:
        app.dependency_overrides[get_session] = session
        app.dependency_overrides.pop(get_current_user, None)
        client = TestClient(app)
        assert client.get("/bgm/tracks").status_code == 401
        loader.assert_not_called()
        app.dependency_overrides[get_current_user] = lambda: "test-user"
        response = client.get("/bgm/tracks", headers={"X-App-Locale": "ko"})
        assert response.status_code == 200
        assert response.json() == {"schema_version": 1, "revision": "a" * 64, "tracks": []}
        assert response.headers["cache-control"] == "private, no-store"
        loader.assert_awaited_once_with("test-session", "ko")
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
