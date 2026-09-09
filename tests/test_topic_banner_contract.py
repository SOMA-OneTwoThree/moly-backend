import copy
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.banner_catalog import BannerCatalog, capabilities, render_feed, select_candidates
from app.services.topic_catalog import TopicCatalog
from tests.test_banner_catalog import manifest


def topic_manifest():
    raw = manifest()
    banner = raw["banners"][0]
    banner["when"] = None
    banner["bindings"] = {"question": {"source": "topic.question", "format": None}}
    canvas = banner["canvases_by_locale"]["en"]
    canvas["elements"][0]["text"]["value"] = "Today's topic"
    canvas["elements"][1]["text"]["value"] = "{question}"
    canvas["elements"][2]["action"] = {"type": "open_topic_conversation_v1"}
    banner["canvases_by_locale"] = {locale: copy.deepcopy(canvas) for locale in ("ko", "en", "ja")}
    return raw


@pytest.mark.parametrize("locale", ["ko", "en", "ja"])
def test_question_and_reference_share_exact_snapshot_and_locale(locale):
    catalog = BannerCatalog.from_bytes(json.dumps(topic_manifest()).encode())
    topics = TopicCatalog.load()
    pointer = topics.manifest.sequence[0]
    offer = SimpleNamespace(offer_id=uuid.uuid4(), offer_sequence=1,
                            topic_id=pointer.topic_id, topic_revision=pointer.topic_revision,
                            questions=topics.questions(pointer).model_dump())
    now = datetime(2026, 9, 7, tzinfo=timezone.utc)
    supported = capabilities(catalog.manifest.banners[0].canvases_by_locale[locale])
    candidates = select_candidates(catalog, now=now, platform="ios", app_version="1.0.0",
                                   locale=locale, supported=supported)
    feed = render_feed(catalog, candidates, now=now, local_date=now.date(),
                       day_ends_at=now, remaining=None, topic_offer=offer)
    assert feed.items[0].canvas.elements[1].text == offer.questions[locale]
    ref = feed.items[0].canvas.elements[2].action.topic_ref
    assert ref.locale == locale
    assert ref.offer_id == offer.offer_id
    assert ref.topic_revision == offer.topic_revision
    assert feed.items[0].data_dependencies == ("topic.question",)
    assert render_feed(catalog, candidates, now=now, local_date=None,
                       day_ends_at=None, remaining=None).items == ()
    assert select_candidates(catalog, now=now, platform="ios", app_version="1.0.0",
                             locale=locale, supported=supported - {"open_topic_conversation_v1"}) == ()


def test_topic_action_requires_exact_question_display():
    raw = topic_manifest()
    raw["banners"][0]["canvases_by_locale"]["en"]["elements"][1]["text"]["value"] = "Hi {question}"
    with pytest.raises(ValueError, match="verbatim"):
        BannerCatalog.from_bytes(json.dumps(raw).encode())


def test_author_cannot_supply_another_users_topic_reference():
    raw = topic_manifest()
    raw["banners"][0]["canvases_by_locale"]["en"]["elements"][2]["action"]["topic_ref"] = {}
    with pytest.raises(ValueError):
        BannerCatalog.from_bytes(json.dumps(raw).encode())
