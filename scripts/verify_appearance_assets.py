"""최종 appearance v2 매니페스트와 원격 이미지 규격을 배포 전에 검증한다."""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
from pathlib import Path

import httpx
from PIL import Image
from pydantic import ValidationError

from app.schemas.shop import ShopProduct

REQUIRED_DEFAULTS = {"theme_default", "theme_workout", "head_sunglasses"}

# 착용 레이어는 번들 캐릭터(cappy.imageset)와 픽셀 단위로 같아야 겹쳐 그렸을 때 정렬된다.
UPRIGHT_LAYER_SIZE = (800, 1100)


def load_products(path: Path) -> list[ShopProduct]:
    raw = json.loads(path.read_text())
    entries = raw.get("products") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ValueError("manifest root must contain a products array")
    products = [ShopProduct.model_validate({**entry, "owned": False, "equipped": False}) for entry in entries]
    ids = [product.id for product in products]
    if len(ids) != len(set(ids)):
        raise ValueError("product ids must be globally unique")
    missing = REQUIRED_DEFAULTS - set(ids)
    if missing:
        raise ValueError(f"required products missing: {sorted(missing)}")
    for product in products:
        version_token = re.compile(rf"(?:^|[/_.-])v{product.asset_version}(?:$|[/_.-])")
        for url in urls_for(product):
            if version_token.search(str(url)) is None:
                raise ValueError(f"{product.id}: URL does not contain v{product.asset_version}: {url}")
    return products


def urls_for(product: ShopProduct) -> list[str]:
    assets = product.assets
    urls = [str(assets.thumbnail_url), str(assets.detail_url)]
    if product.slot == "theme":
        assert assets.scene is not None
        urls.append(str(assets.scene.character_url))
        for layer in assets.scene.layers:
            urls.append(str(layer.day_url))
            if layer.night_url is not None:
                urls.append(str(layer.night_url))
    else:
        assert assets.upright_layer_url is not None
        urls.append(str(assets.upright_layer_url))
    return urls


async def fetch_image(client: httpx.AsyncClient, url: str) -> Image.Image:
    response = await client.get(url)
    response.raise_for_status()
    if not response.content:
        raise ValueError(f"empty image: {url}")
    try:
        image = Image.open(io.BytesIO(response.content))
        image.load()
        return image
    except Exception as exc:
        raise ValueError(f"image decode failed: {url}") from exc


def require_transparent_png(image: Image.Image, size: tuple[int, int], label: str) -> None:
    if image.format != "PNG" or image.size != size:
        raise ValueError(f"{label}: expected transparent PNG {size}, got {image.format} {image.size}")
    if "A" not in image.getbands() and "transparency" not in image.info:
        raise ValueError(f"{label}: PNG has no alpha channel")
    alpha = image.convert("RGBA").getchannel("A")
    if alpha.getextrema()[0] == 255:
        raise ValueError(f"{label}: PNG has no transparent pixels")


async def verify_remote(products: list[ShopProduct]) -> None:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for product in products:
            for url in urls_for(product):
                await fetch_image(client, url)
            if product.slot != "theme":
                upright = await fetch_image(client, str(product.assets.upright_layer_url))
                require_transparent_png(upright, UPRIGHT_LAYER_SIZE, f"{product.id}.upright")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--skip-fetch", action="store_true", help="DTO와 URL 버전만 검증")
    args = parser.parse_args()
    try:
        products = load_products(args.manifest)
        if not args.skip_fetch:
            asyncio.run(verify_remote(products))
    except (OSError, ValueError, ValidationError, httpx.HTTPError) as exc:
        raise SystemExit(f"appearance asset verification failed: {exc}") from exc
    print(f"appearance asset verification passed: {len(products)} products")


if __name__ == "__main__":
    main()
