import hashlib
from io import BytesIO

import pytest
from PIL import Image

from app.schemas.banners import BannerImageSource
from app.services.banner_assets import validate_image_bytes


def asset(format="PNG", animated=False):
    image = Image.new("RGB", (16, 16), "red")
    buffer = BytesIO()
    options = {}
    if animated:
        options = dict(
            save_all=True, append_images=[Image.new("RGB", (16, 16), "blue")], duration=100
        )
    image.save(buffer, format=format, **options)
    raw = buffer.getvalue()
    source = BannerImageSource(
        url="https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/assets/banner.png",
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_length=len(raw),
        pixel_width=16,
        pixel_height=16,
        media_type=Image.MIME[format],
    )
    return source, raw


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
def test_valid_static_image(format):
    source, raw = asset(format)
    validate_image_bytes(source, raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"sha256": "0" * 64},
        {"byte_length": 1},
        {"pixel_width": 17},
        {"media_type": "image/jpeg"},
    ],
)
def test_declared_metadata_must_match_actual_bytes(changes):
    source, raw = asset()
    with pytest.raises(ValueError):
        validate_image_bytes(source.model_copy(update=changes), raw)


@pytest.mark.parametrize("format", ["PNG", "WEBP"])
def test_animation_rejected(format):
    source, raw = asset(format, animated=True)
    with pytest.raises(ValueError):
        validate_image_bytes(source, raw)


@pytest.mark.parametrize(
    "url",
    [
        "http://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/a/b.png",
        "https://attacker.example/x.png",
        "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/sign/a/b.png?token=secret",
        "https://user:password@qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/a/b.png",
    ],
)
def test_url_policy(url):
    source, _ = asset()
    raw = source.model_dump()
    raw["url"] = url
    with pytest.raises(ValueError):
        BannerImageSource.model_validate(raw)
