"""Read-only publication gate: validate metadata and downloaded bytes before DB activation."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import httpx
from app.schemas.banners import PRODUCTION_ASSET_ORIGIN
from app.schemas.bgm import BgmTrack


async def verify(track: BgmTrack, *, production: bool = False,
                 client: httpx.AsyncClient | None = None) -> None:
    if track.source != "remote":
        raise ValueError("publication requires a remote track")
    if production and not track.url.startswith(PRODUCTION_ASSET_ORIGIN + "/"):
        raise ValueError("production requires production Storage")
    if client is None:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as owned:
            return await verify(track, production=production, client=owned)
    digest, size = hashlib.sha256(), 0
    async with client.stream("GET", track.url, headers={"Accept-Encoding": "identity"}) as response:
        if response.status_code != 200:
            raise ValueError("asset must return 200 without redirects")
        if response.headers.get("content-type", "").split(";")[0].strip() != track.mime_type:
            raise ValueError("audio MIME mismatch")
        if response.headers.get("content-encoding", "identity") != "identity":
            raise ValueError("encoded audio response is unsupported")
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > track.size_bytes:
                raise ValueError("audio exceeds declared size")
            digest.update(chunk)
    if size != track.size_bytes or digest.hexdigest() != track.sha256:
        raise ValueError("audio size/hash mismatch")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path, help="JSON object matching one API track")
    parser.add_argument("--production", action="store_true")
    args = parser.parse_args()
    track = BgmTrack.model_validate(json.loads(args.metadata.read_text()))
    asyncio.run(verify(track, production=args.production))
    print(f"Verified {track.id} revision {track.revision}; no database changes made")


if __name__ == "__main__":
    main()
