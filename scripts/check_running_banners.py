"""Run inside the deployed container; verify the process loaded this image's catalog."""

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.banner_catalog import BannerCatalog  # noqa: E402


def main() -> None:
    expected = BannerCatalog.load()
    token = os.environ.get("HEALTH_TOKEN", "")
    if not token:
        raise SystemExit("Banner diagnostic requires configured HEALTH_TOKEN")
    request = urllib.request.Request(
        "http://127.0.0.1:8000/health/banners", headers={"X-Health-Token": token}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        raw = response.read(4097)
    if len(raw) > 4096:
        raise SystemExit("Unexpected banner diagnostic response")
    actual = json.loads(raw)
    if actual.get("status") != "ok" or actual.get("revision") != expected.revision:
        raise SystemExit("Running banner revision differs from the deployed image")
    print(json.dumps({"status": "ok", "revision": expected.revision}))


if __name__ == "__main__":
    main()
