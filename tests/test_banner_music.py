import copy
import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.services.banner_catalog import CATALOG_PATH, BannerCatalog, binding_values, capabilities
from app.services.banner_music import MUSIC_TRACKS, daily_music_title
from app.services.banners import list_banners
from app.core.app_day import AppDay


def music_test_catalog():
    """Exercise the supported binding independently of the published banner lineup."""
    raw = json.loads(CATALOG_PATH.read_text())
    music = copy.deepcopy(next(b for b in raw['banners'] if b['id'] == 'mood-daily'))
    music['id'] = 'music-daily'
    music['bindings'] = {'track': {'source': 'music.daily_title', 'format': None}}
    for canvas in music['canvases_by_locale'].values():
        for element in canvas['elements']:
            if element['id'] == 'message':
                element['text'] = {'kind': 'template', 'value': '{track}'}
            if element['id'] == 'primary-action':
                element['action'] = {'type': 'open_music'}
    raw['banners'].append(music)
    return BannerCatalog.from_bytes(json.dumps(raw).encode())


def test_music_is_stable_across_languages_and_covers_available_tracks():
    banner = next(b for b in music_test_catalog().manifest.banners if b.id == 'music-daily')
    seen = set()
    for offset in range(90):
        day = date(2026, 9, 9) + timedelta(days=offset)
        title = daily_music_title(day)
        for locale in ('ko', 'en', 'ja'):
            assert binding_values(banner, locale, day, None)['track'] == title
        seen.add(title)
    assert seen == {title for _, title in MUSIC_TRACKS}
    with pytest.raises(ValueError, match='music date unavailable'):
        binding_values(banner, 'en', None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize('zone', ['Asia/Seoul', 'America/Los_Angeles'])
async def test_music_uses_local_date_and_existing_wire_dependencies_without_db(zone):
    catalog = music_test_catalog()
    caps = frozenset().union(*(capabilities(c) for b in catalog.manifest.banners
                              for c in b.canvases_by_locale.values())) - {'open_topic_conversation_v1'}
    now = datetime(2026, 9, 9, 15, 30, tzinfo=timezone.utc)
    session = AsyncMock()
    feed = await list_banners(catalog, session, '00000000-0000-0000-0000-000000000001',
                             now=now, platform='ios', app_version='1.0.0', locale='ko',
                             timezone_name=zone, capabilities=caps)
    music = next(b for b in feed.items if b.id == 'music-daily')
    mood = next(b for b in feed.items if b.id == 'mood-daily')
    day = AppDay.at(now, zone)
    assert next(e.text for e in music.canvas.elements if e.id == 'message') == daily_music_title(day.local_date)
    assert music.valid_until == day.ends_at
    assert music.data_dependencies == ('user.local_date',)
    assert next(e.action.type for e in music.canvas.elements if e.id == 'primary-action') == 'open_music'
    assert next(e.action.type for e in mood.canvas.elements if e.id == 'primary-action') == 'open_mood'
    session.execute.assert_not_called()
    unsupported = await list_banners(catalog, session, '00000000-0000-0000-0000-000000000001',
                                    now=now, platform='ios', app_version='1.0.0', locale='en',
                                    timezone_name=zone, capabilities=caps - {'open_music', 'open_mood'})
    assert not {'music-daily', 'mood-daily'} & {b.id for b in unsupported.items}


def test_current_cards_fit_feed_budget_and_existing_dependency_contract():
    from types import SimpleNamespace
    from uuid import UUID
    from app.services.banner_catalog import render_feed

    catalog = BannerCatalog.load()
    now = datetime(2026, 9, 9, tzinfo=timezone.utc)
    for locale in ('ko', 'en', 'ja'):
        feed = render_feed(catalog, [(b, locale, b.canvases_by_locale[locale])
                                    for b in catalog.manifest.banners], now=now,
                           local_date=now.date(), day_ends_at=now + timedelta(days=1),
                           remaining=1, topic_offer=SimpleNamespace(
                               questions={locale: 'Question?'}, offer_id=UUID(int=1),
                               offer_sequence=1, topic_id='sample', topic_revision='0' * 64))
        assert [b.id for b in feed.items] == [
            'composed-image-test', 'mood-daily', 'shop-new-items', 'purple-image-test'
        ]
        assert len(feed.model_dump_json().encode()) < 128 * 1024
        assert all(set(b.data_dependencies) <= {'user.local_date', 'topic.question',
                                               'routines.remaining_today'} for b in feed.items)
