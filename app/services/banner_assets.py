"""Deployment-time asset verification; never called during a user request."""

import hashlib
from io import BytesIO

import httpx
from PIL import Image

from app.schemas.banners import BannerImageSource


def validate_image_bytes(source: BannerImageSource, raw: bytes) -> None:
    if len(raw) != source.byte_length or len(raw) > 512 * 1024:
        raise ValueError("image byte length mismatch")
    if hashlib.sha256(raw).hexdigest() != source.sha256:
        raise ValueError("image hash mismatch")
    with Image.open(BytesIO(raw)) as image:
        if image.format not in {"PNG", "JPEG", "WEBP"}:
            raise ValueError("unsupported image format")
        if Image.MIME.get(image.format) != source.media_type:
            raise ValueError("image MIME mismatch")
        if image.size != (source.pixel_width, source.pixel_height):
            raise ValueError("image dimensions mismatch")
        if getattr(image, "n_frames", 1) != 1 or getattr(image, "is_animated", False):
            raise ValueError("animated images are unsupported")
        image.verify()
    with Image.open(BytesIO(raw)) as image:
        image.load()


async def validate_remote_image(source: BannerImageSource) -> None:
    # A fresh, proxy-independent client prevents API credentials and cookie reuse.
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=5) as client:
        async with client.stream("GET", source.url) as response:
            if response.status_code != 200:
                raise ValueError("asset must return 200 without redirects")
            if (
                response.headers.get("content-type", "").split(";", 1)[0].strip()
                != source.media_type
            ):
                raise ValueError("asset Content-Type mismatch")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > source.byte_length or len(raw) > 512 * 1024:
                    raise ValueError("asset stream exceeds byte budget")
    validate_image_bytes(source, bytes(raw))
