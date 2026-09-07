"""Validate the catalog in this checkout/image before deployment."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from datetime import date
from uuid import UUID
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.banner_catalog import BannerCatalog  # noqa: E402
from app.services.banner_catalog import binding_values, compile_canvas  # noqa: E402
from app.services.topic_catalog import TopicCatalog  # noqa: E402
from app.schemas.topics import TopicReference  # noqa: E402


def validate_topics(catalog: BannerCatalog, topics: TopicCatalog) -> int:
    count = 0
    for banner in catalog.manifest.banners:
        if not any(binding.source == "topic.question" for binding in banner.bindings.values()):
            continue
        for (topic_id, revision), questions in topics.versions.items():
            for locale, canvas in banner.canvases_by_locale.items():
                values = binding_values(banner, locale, date(2026, 12, 31), 999,
                                        topic_question=getattr(questions, locale))
                compile_canvas(canvas, values, topic_ref=TopicReference(
                    offer_id=UUID(int=1), offer_sequence=1, topic_id=topic_id,
                    topic_revision=revision, locale=locale,
                ))
                count += 1
    return count


async def validate_assets(catalog: BannerCatalog, environment: str | None = None) -> int:
    from app.services.banner_assets import validate_remote_image

    from app.schemas.banners import PRODUCTION_ASSET_ORIGIN

    sources = {}

    def add(source):
        parsed = urlsplit(source.url)
        if environment == "prod" and f"{parsed.scheme}://{parsed.netloc}" != PRODUCTION_ASSET_ORIGIN:
            raise ValueError("production deployment cannot reference development banner assets")
        key = (source.url, source.sha256)
        if key in sources and sources[key] != source:
            raise ValueError("conflicting image metadata for the same content")
        sources[key] = source

    for banner in catalog.manifest.banners:
        for canvas in banner.canvases_by_locale.values():
            if canvas.background.type == "image_background_v1":
                source = canvas.background.source
                add(source)
            for element in canvas.elements:
                if element.type == "image_v1":
                    add(element.source)
    semaphore = asyncio.Semaphore(2)

    async def check(source):
        async with semaphore, asyncio.timeout(5):
            await validate_remote_image(source)

    await asyncio.gather(*(check(s) for s in sources.values()))
    return len(sources)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", action="store_true")
    parser.add_argument("--environment", choices=("dev", "prod"))
    parser.add_argument("--previous-topics", type=Path,
                        help="Previously deployed catalog; enforce append-only compatible updates")
    args = parser.parse_args()
    catalog = BannerCatalog.load()
    topics = TopicCatalog.load()
    if args.previous_topics:
        topics.validate_update(TopicCatalog.load(args.previous_topics))
    topic_variants = validate_topics(catalog, topics)
    count = asyncio.run(validate_assets(catalog, args.environment)) if args.assets else None
    print(
        json.dumps(
            {
                "status": "ok",
                "revision": catalog.revision,
                "enabled": catalog.manifest.enabled,
                "banners": len(catalog.manifest.banners),
                "verified_assets": count,
                "topic_revision": topics.revision,
                "topic_variants": topic_variants,
            }
        )
    )


if __name__ == "__main__":
    main()
