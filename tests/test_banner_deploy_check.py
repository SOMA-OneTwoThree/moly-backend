import io
import json
from types import SimpleNamespace

import pytest

from scripts import check_running_banners


@pytest.mark.parametrize("revision", ["expected", "wrong"])
def test_running_catalog_must_match_image(monkeypatch, capsys, revision):
    monkeypatch.setenv("HEALTH_TOKEN", "test-only-token")
    monkeypatch.setattr(
        check_running_banners.BannerCatalog, "load",
        lambda: SimpleNamespace(revision="expected"),
    )

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "http://127.0.0.1:8000/health/banners"
            assert request.get_header("X-health-token") == "test-only-token"
            assert timeout == 5
            return io.BytesIO(json.dumps({"status": "ok", "revision": revision}).encode())

    monkeypatch.setattr(check_running_banners.urllib.request, "build_opener", lambda _: Opener())
    if revision == "wrong":
        with pytest.raises(SystemExit, match="differs"):
            check_running_banners.main()
    else:
        check_running_banners.main()
    assert "test-only-token" not in capsys.readouterr().out


def test_missing_health_token_fails_without_network(monkeypatch):
    monkeypatch.delenv("HEALTH_TOKEN", raising=False)
    monkeypatch.setattr(
        check_running_banners.BannerCatalog, "load", lambda: SimpleNamespace(revision="expected")
    )
    with pytest.raises(SystemExit, match="requires configured"):
        check_running_banners.main()
