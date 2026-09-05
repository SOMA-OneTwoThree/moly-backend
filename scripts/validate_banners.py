"""Validate the catalog in this checkout/image before deployment."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.banner_catalog import BannerCatalog  # noqa: E402


async def validate_assets(catalog: BannerCatalog) -> int:
    from app.services.banner_assets import validate_remote_image

    sources = {}

    def add(source):
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
    args = parser.parse_args()
    catalog = BannerCatalog.load()
    count = asyncio.run(validate_assets(catalog)) if args.assets else None
    print(
        json.dumps(
            {
                "status": "ok",
                "revision": catalog.revision,
                "enabled": catalog.manifest.enabled,
                "banners": len(catalog.manifest.banners),
                "verified_assets": count,
            }
        )
    )


if __name__ == "__main__":
    main()
