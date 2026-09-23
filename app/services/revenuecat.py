"""RevenueCat v2 read-only subscription lookup. Never log credentials or response bodies."""
from __future__ import annotations

from urllib.parse import quote, urlencode, urljoin

import httpx

from app.config import settings


class RevenueCatError(Exception):
    """Sanitized, retryable reconciliation failure."""


async def _get(path: str, *, missing_ok: bool = False) -> dict:
    if not settings.revenuecat_api_v2_key or not settings.revenuecat_project_id:
        raise RevenueCatError("RevenueCat read credentials unavailable")
    prefix = "https://api.revenuecat.com/v2/projects/" + quote(settings.revenuecat_project_id, safe="") + "/"
    url = urljoin(prefix, path)
    if not url.startswith(prefix):
        raise RevenueCatError("RevenueCat pagination outside project")
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            response = await client.get(url, headers={"Authorization": "Bearer " + settings.revenuecat_api_v2_key})
        if response.status_code == 404 and missing_ok:
            return {"items": []}
        if response.status_code != 200:
            raise RevenueCatError(f"RevenueCat read HTTP {response.status_code}")
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (httpx.RequestError, ValueError):
        raise RevenueCatError("RevenueCat read transport or payload failure") from None


async def _list(path: str, *, missing_ok: bool = False) -> list[dict]:
    rows = []
    for _ in range(10):
        body = await _get(path, missing_ok=missing_ok)
        missing_ok = False
        items = body.get("items")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise RevenueCatError("RevenueCat invalid list")
        rows.extend(items)
        path = body.get("next_page")
        if not path:
            return rows
    raise RevenueCatError("RevenueCat list incomplete; reconciliation deferred")


async def customer_subscriptions(uid: str) -> list[dict]:
    return await _list("customers/" + quote(uid, safe="") + "/subscriptions?limit=100", missing_ok=True)


async def search_subscriptions(store_tx: str) -> list[dict]:
    return await _list("subscriptions?" + urlencode({"store_subscription_identifier": store_tx}))


async def get_subscription(rc_sub_id: str) -> dict:
    return await _get("subscriptions/" + quote(rc_sub_id, safe=""))


async def subscription_transactions(rc_sub_id: str) -> list[dict]:
    return await _list("subscriptions/" + quote(rc_sub_id, safe="") + "/transactions?limit=100&sort=purchased_at&direction=asc")
