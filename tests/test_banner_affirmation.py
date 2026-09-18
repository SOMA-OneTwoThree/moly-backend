from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.app_day import AppDay
from app.services.banner_catalog import (
    BannerCatalog,
    binding_values,
    capabilities,
    render_feed,
    select_candidates,
)
from app.services.banners import list_banners
from tests.test_banner_catalog import composed_action_manifest, load

NOW = datetime(2026, 9, 9, 15, 30, tzinfo=timezone.utc)
USER = "00000000-0000-0000-0000-000000000001"


class _Savepoint:
    """AsyncSession.begin_nested()의 async context manager 대역."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def affirmation_manifest():
    """루틴 대역을 오늘의 글귀 binding으로 바꾼 최소 manifest."""
    raw = composed_action_manifest()
    banner = raw["banners"][0]
    banner["bindings"] = {
        "acknowledged": {"source": "affirmation.acknowledged_today", "format": None}
    }
    banner["when"] = {"binding": "acknowledged", "operator": "eq", "value": 0}
    canvas = banner["canvases_by_locale"]["en"]
    canvas["elements"][1]["text"] = {"kind": "template", "value": "Reveal your daily affirmation"}
    canvas["elements"][2]["action"] = {"type": "open_affirmation"}
    return raw


def feed_for(acknowledged, raw=None):
    catalog = load(raw or affirmation_manifest())
    canvas = catalog.manifest.banners[0].canvases_by_locale["en"]
    candidates = select_candidates(catalog, now=NOW, platform="ios", app_version="1.0.0",
                                   locale="en", supported=capabilities(canvas))
    return render_feed(catalog, candidates, now=NOW, local_date=NOW.date(),
                       day_ends_at=NOW, remaining=None, acknowledged=acknowledged)


@pytest.mark.parametrize("acknowledged,expected", [(0, 1), (1, 0), (None, 0)])
def test_card_is_hidden_once_the_sentence_is_acknowledged(acknowledged, expected):
    assert len(feed_for(acknowledged).items) == expected


def test_card_declares_the_new_dependency_and_expires_at_local_midnight():
    card = feed_for(0).items[0]
    assert card.data_dependencies == ("affirmation.acknowledged_today",)
    assert card.valid_until == NOW
    assert card.canvas.elements[2].action.type == "open_affirmation"


def test_binding_requires_a_resolved_value():
    banner = load(affirmation_manifest()).manifest.banners[0]
    assert binding_values(banner, "en", NOW.date(), None, acknowledged=1) == {"acknowledged": 1}
    with pytest.raises(ValueError, match="affirmation binding unavailable"):
        binding_values(banner, "en", NOW.date(), None)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda b: b.update(when=None),
        lambda b: b.update(when={"binding": "acknowledged", "operator": "gt", "value": 0}),
        lambda b: b.update(when={"binding": "acknowledged", "operator": "eq", "value": 1}),
    ],
)
def test_affirmation_banner_requires_acknowledged_equals_zero(mutation):
    raw = affirmation_manifest()
    mutation(raw["banners"][0])
    with pytest.raises(ValueError, match="affirmation-dependent banners require acknowledged"):
        load(raw)


def test_condition_still_rejects_a_date_binding():
    raw = affirmation_manifest()
    banner = raw["banners"][0]
    banner["bindings"]["today"] = {"source": "user.local_date", "format": "month_day"}
    banner["when"] = {"binding": "today", "operator": "eq", "value": 0}
    with pytest.raises(ValueError, match="condition requires integer binding"):
        load(raw)


def test_published_card_keeps_its_authored_shape():
    catalog = BannerCatalog.load()
    banner = next(b for b in catalog.manifest.banners if b.id == "affirmation-daily")
    assert banner.when.operator == "eq" and banner.when.value == 0
    for locale, canvas in banner.canvases_by_locale.items():
        assert {e.id for e in canvas.elements} == {
            "heading", "heading-divider", "message", "action-surface", "action-label", "primary-action"
        }
        region = next(e for e in canvas.elements if e.id == "primary-action")
        assert region.action.type == "open_affirmation_screen_v1"
        fortune = next(b for b in catalog.manifest.banners if b.id == "composed-image-test")
        reference = {e.id: e for e in fortune.canvases_by_locale[locale].elements}
        for element_id in ("action-surface", "action-label", "primary-action"):
            element = next(e for e in canvas.elements if e.id == element_id)
            assert element.frame == reference[element_id].frame
        assert "open_affirmation_screen_v1" in capabilities(canvas)
        assert "affirmation.text" not in {b.source for b in banner.bindings.values()}
        assert locale in {"en", "ko", "ja"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "acknowledged_at,present",
    [(None, True), (datetime(2026, 9, 9, 1, tzinfo=timezone.utc), False)],
)
async def test_list_banners_reads_the_daily_marker(acknowledged_at, present):
    catalog = BannerCatalog.load()
    caps = frozenset().union(*(capabilities(c) for b in catalog.manifest.banners
                               for c in b.canvases_by_locale.values()))
    session = AsyncMock()
    session.begin_nested = lambda: _Savepoint()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar=lambda: acknowledged_at))
    feed = await list_banners(catalog, session, USER, now=NOW, platform="ios",
                              app_version="1.0.0", locale="ko", timezone_name="Asia/Seoul",
                              capabilities=caps)
    ids = {card.id for card in feed.items}
    assert ("affirmation-daily" in ids) is present
    session.execute.assert_awaited_once()
    if present:
        card = next(card for card in feed.items if card.id == "affirmation-daily")
        assert card.valid_until == AppDay.at(NOW, "Asia/Seoul").ends_at
        assert card.data_dependencies == ("affirmation.acknowledged_today",)


def test_inline_sentence_contains_displayed_date_and_requires_new_capability():
    from app.services.affirmation import daily_affirmation

    raw = affirmation_manifest()
    banner = raw["banners"][0]
    banner["bindings"]["sentence"] = {"source": "affirmation.text", "format": None}
    canvas = banner["canvases_by_locale"]["en"]
    canvas["elements"][1]["text"] = {"kind": "template", "value": "{sentence}"}
    canvas["elements"][2]["action"] = {"type": "acknowledge_affirmation_v1"}
    feed = feed_for(0, raw)
    card = feed.items[0]
    assert card.canvas.elements[1].text == daily_affirmation(NOW.date()).text.en
    assert card.canvas.elements[2].action.local_date == NOW.date()
    assert "affirmation.text" in card.data_dependencies
    assert not feed_for(1, raw).items
    catalog = load(raw)
    supported = capabilities(catalog.manifest.banners[0].canvases_by_locale["en"])
    assert not select_candidates(
        catalog, now=NOW, platform="ios", app_version="1.0.0", locale="en",
        supported=(supported - {"acknowledge_affirmation_v1"}) | {"open_affirmation"},
    )


def test_fullscreen_entry_requires_its_own_capability_and_does_not_acknowledge():
    catalog = BannerCatalog.load()
    banner = next(b for b in catalog.manifest.banners if b.id == "affirmation-daily")
    canvas = banner.canvases_by_locale["ko"]
    caps = capabilities(canvas)
    def candidates(supported):
        return select_candidates(catalog, now=NOW, platform="ios", app_version="1.1.7",
                                 locale="ko", supported=supported)
    assert not any(b.id == "affirmation-daily" for b, _ in candidates(
        (caps - {"open_affirmation_screen_v1"}) | {"acknowledge_affirmation_v1"}))
    feed = render_feed(catalog, candidates(caps), now=NOW, local_date=NOW.date(),
                       day_ends_at=NOW, remaining=None, acknowledged=0)
    card = next(c for c in feed.items if c.id == "affirmation-daily")
    assert next(e for e in card.canvas.elements if e.id == "message").text == "오늘의 문장을 확인하세요!"
    assert card.canvas.elements[-1].action.local_date == NOW.date()
