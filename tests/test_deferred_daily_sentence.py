"""The undecided daily sentence feature is absent from the server release."""
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import create_app
from app.schemas.banners import BannerBinding
from app.services.banner_catalog import BannerCatalog


def test_daily_sentence_endpoints_are_not_exposed(monkeypatch):
    monkeypatch.setattr(settings, 'environment', 'local')
    app = create_app()
    client = TestClient(app)
    assert client.get('/daily-affirmation').status_code == 404
    assert client.post('/daily-affirmation/acknowledge', json={
        'local_date': '2026-09-23',
    }).status_code == 404
    assert not any('daily-affirmation' in path for path in app.openapi()['paths'])


def test_catalog_cannot_reintroduce_deferred_sentence_bindings():
    catalog = BannerCatalog.load()
    assert all(b.id != 'affirmation-daily' for b in catalog.manifest.banners)
    for source in ['affirmation.text', 'affirmation.acknowledged_today']:
        with pytest.raises(ValueError):
            BannerBinding(source=source, format=None)
