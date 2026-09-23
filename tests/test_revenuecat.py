"""RevenueCat read client: authenticated GET only, bounded complete pagination."""
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services import revenuecat


@pytest.mark.parametrize("status", [200, 401, 403, 429, 500])
async def test_only_get_and_sanitized_errors(monkeypatch, status):
    monkeypatch.setattr(revenuecat.settings, "revenuecat_api_v2_key", "test-private-key")
    monkeypatch.setattr(revenuecat.settings, "revenuecat_project_id", "project")
    real_client = httpx.AsyncClient
    def route(request):
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer test-private-key"
        return httpx.Response(status, json={"items": [], "secret": "do-not-echo"})
    monkeypatch.setattr(revenuecat.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(route), **kwargs))
    if status == 200:
        assert await revenuecat.customer_subscriptions("user/a") == []
    else:
        with pytest.raises(revenuecat.RevenueCatError) as error:
            await revenuecat.customer_subscriptions("user/a")
        assert str(status) in str(error.value)
        assert "test-private-key" not in str(error.value) and "do-not-echo" not in str(error.value)


async def test_customer_missing_ok_does_not_hide_missing_next_page(monkeypatch):
    get = AsyncMock(side_effect=[{"items": [{"id": "one"}], "next_page": "next"}, revenuecat.RevenueCatError("404")])
    monkeypatch.setattr(revenuecat, "_get", get)
    with pytest.raises(revenuecat.RevenueCatError):
        await revenuecat.customer_subscriptions("one")
    assert get.await_args_list[0].kwargs == {"missing_ok": True}
    assert get.await_args_list[1].kwargs == {"missing_ok": False}


async def test_cross_project_pagination_rejected_before_network(monkeypatch):
    monkeypatch.setattr(revenuecat.settings, "revenuecat_api_v2_key", "test-private-key")
    monkeypatch.setattr(revenuecat.settings, "revenuecat_project_id", "project")
    with pytest.raises(revenuecat.RevenueCatError, match="outside project"):
        await revenuecat._get("https://attacker.example/subscriptions")


async def test_truncated_history_never_returns_partial_success(monkeypatch):
    get = AsyncMock(return_value={"items": [{"id": "one"}], "next_page": "next"})
    monkeypatch.setattr(revenuecat, "_get", get)
    with pytest.raises(revenuecat.RevenueCatError, match="incomplete"):
        await revenuecat.subscription_transactions("sub")
    assert get.await_count == 10
