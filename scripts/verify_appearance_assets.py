"""최종 appearance 매니페스트와 원격 이미지 규격을 배포 전에 검증한다.

매니페스트는 DB 진실(slot=hat/glasses/…, 착용 assets에 rightside 포함)을 담는다.
각 상품을 v2 계약과 레거시(구버전) 투영 양쪽으로 검증해 구버전 앱 보호를 배포 전에 강제한다.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
from pathlib import Path
from typing import Any

import httpx
from PIL import Image
from pydantic import ValidationError

from app.schemas.shop import ShopProduct, ShopProductV2
from app.services.shop import legacy_asset_view, rightside_asset_view

REQUIRED_DEFAULTS = {"theme_default", "theme_workout", "head_sunglasses"}

# 착용 레이어는 번들 캐릭터(cappy.imageset)와 픽셀 단위로 같아야 겹쳐 그렸을 때 정렬된다.
# rightside 자세도 같은 캔버스를 쓴다 — 새 자세 캔버스 규격이 달라지면 이 값을 갱신한다.
UPRIGHT_LAYER_SIZE = (800, 1100)


def _legacy_slot(slot: str) -> str:
    return "head" if slot in ("hat", "glasses") else slot


def load_products(path: Path) -> list[ShopProductV2]:
    raw = json.loads(path.read_text())
    entries = raw.get("products") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ValueError("manifest root must contain a products array")
    products: list[ShopProductV2] = []
    for entry in entries:
        assets = entry["assets"]
        product = ShopProductV2.model_validate(
            {**entry, "assets": rightside_asset_view(assets), "owned": False, "equipped": False}
        )
        # 레거시 투영도 반드시 유효해야 한다 — 구버전 앱이 이 응답 형태를 계속 받는다.
        ShopProduct.model_validate(
            {
                **entry,
                "slot": _legacy_slot(entry["slot"]),
                "assets": legacy_asset_view(assets),
                "owned": False,
                "equipped": False,
            }
        )
        # 착용 아이템은 rightside 자세 레이어를 반드시 제공해야 한다(런타임 폴백에 기대지 않는다).
        if entry["slot"] != "theme" and not (assets.get("rightside") or {}).get("upright_layer_url"):
            raise ValueError(f"{entry['id']}: wearable is missing rightside.upright_layer_url")
        products.append(product)
    ids = [product.id for product in products]
    if len(ids) != len(set(ids)):
        raise ValueError("product ids must be globally unique")
    missing = REQUIRED_DEFAULTS - set(ids)
    if missing:
        raise ValueError(f"required products missing: {sorted(missing)}")
    for entry, product in zip(entries, products):
        version_token = re.compile(rf"(?:^|[/_.-])v{product.asset_version}(?:$|[/_.-])")
        for url in urls_for(entry["assets"]):
            if version_token.search(url) is None:
                raise ValueError(f"{product.id}: URL does not contain v{product.asset_version}: {url}")
    return products


def urls_for(assets: dict[str, Any]) -> list[str]:
    """DB assets(구 자세 + rightside)의 모든 이미지 URL을 모은다."""
    urls: list[str] = []
    for key in ("thumbnail_url", "detail_url", "upright_layer_url"):
        if assets.get(key):
            urls.append(assets[key])
    rightside = assets.get("rightside") or {}
    if rightside.get("upright_layer_url"):
        urls.append(rightside["upright_layer_url"])
    scene = assets.get("scene")
    if scene:
        urls.append(scene["character_url"])
        for layer in scene["layers"]:
            urls.append(layer["day_url"])
            if layer.get("night_url"):
                urls.append(layer["night_url"])
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


async def verify_remote(entries: list[dict[str, Any]]) -> None:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for entry in entries:
            assets = entry["assets"]
            for url in urls_for(assets):
                await fetch_image(client, url)
            if entry["slot"] != "theme":
                # 구 자세와 rightside 자세 둘 다 번들 캐릭터와 정렬되는 투명 PNG여야 한다.
                poses = {
                    "upright": assets["upright_layer_url"],
                    "rightside": assets["rightside"]["upright_layer_url"],
                }
                for label, url in poses.items():
                    image = await fetch_image(client, url)
                    require_transparent_png(image, UPRIGHT_LAYER_SIZE, f"{entry['id']}.{label}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--skip-fetch", action="store_true", help="DTO와 URL 버전만 검증")
    args = parser.parse_args()
    try:
        products = load_products(args.manifest)
        if not args.skip_fetch:
            entries = json.loads(args.manifest.read_text())["products"]
            asyncio.run(verify_remote(entries))
    except (OSError, ValueError, ValidationError, httpx.HTTPError) as exc:
        raise SystemExit(f"appearance asset verification failed: {exc}") from exc
    print(f"appearance asset verification passed: {len(products)} products")


if __name__ == "__main__":
    main()
