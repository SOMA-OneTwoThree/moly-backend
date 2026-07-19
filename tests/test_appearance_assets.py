import json

import pytest

from scripts.verify_appearance_assets import load_products


def _wearable(product_id: str, version: int = 1, slot: str = "glasses") -> dict:
    root = f"https://cdn.example.com/{product_id}/v{version}"
    return {
        "id": product_id,
        "name": product_id,
        "slot": slot,
        "price_hay": 1000,
        "asset_version": version,
        "assets": {
            "thumbnail_url": f"{root}/thumb.png",
            "detail_url": f"{root}/detail.png",
            "upright_layer_url": f"{root}/upright.png",
            "rightside": {"upright_layer_url": f"{root}/rightside/upright.png"},
        },
    }


def _theme(product_id: str, price_hay: int | None) -> dict:
    root = f"https://cdn.example.com/{product_id}/v1"
    return {
        "id": product_id,
        "name": product_id,
        "slot": "theme",
        "price_hay": price_hay,
        "asset_version": 1,
        "assets": {
            "thumbnail_url": f"{root}/thumb.png",
            "detail_url": f"{root}/detail.png",
            "scene": {
                "canvas": {"width": 393, "height": 852},
                "character_frame": {"x": 51, "y": 338, "width": 171, "height": 85},
                "character_url": f"{root}/character.png",
                "layers": [{
                    "id": "background",
                    "frame": {"x": 0, "y": 0, "width": 393, "height": 852},
                    "z_index": 0,
                    "day_url": f"{root}/background.png",
                }],
            },
        },
    }


def _manifest() -> dict:
    return {
        "products": [
            _theme("theme_default", None),
            _theme("theme_workout", 4000),
            _wearable("head_sunglasses"),
        ]
    }


def test_manifest_static_contract_passes(tmp_path):
    path = tmp_path / "appearance.json"
    path.write_text(json.dumps(_manifest()))
    products = load_products(path)
    assert {product.id for product in products} == {
        "theme_default", "theme_workout", "head_sunglasses",
    }


def test_manifest_rejects_unversioned_url(tmp_path):
    manifest = _manifest()
    manifest["products"][2]["assets"]["upright_layer_url"] = (
        "https://cdn.example.com/head_sunglasses/upright.png"
    )
    path = tmp_path / "appearance.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="does not contain v1"):
        load_products(path)


def test_manifest_rejects_missing_default_product(tmp_path):
    manifest = _manifest()
    manifest["products"] = manifest["products"][:-1]
    path = tmp_path / "appearance.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="required products missing"):
        load_products(path)


def test_manifest_rejects_wearable_without_rightside(tmp_path):
    """착용 아이템은 rightside 자세 레이어가 반드시 있어야 한다."""
    manifest = _manifest()
    del manifest["products"][2]["assets"]["rightside"]
    path = tmp_path / "appearance.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="missing rightside"):
        load_products(path)
