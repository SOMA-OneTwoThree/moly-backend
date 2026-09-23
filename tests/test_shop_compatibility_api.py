"""Mixed-version requests against real shop services, with only DB access replaced."""
from fastapi.testclient import TestClient
import pytest

from app.core.db import get_session
from app.core.security import get_current_user
from app.main import app
from app.services import shop
from tests.test_crud import (
    UID, _CATALOG_PROFILE, FakeSession, _item, _row, _theme, _timer_clothing,
)

CAPABLE = {"X-Moly-Capabilities": "future-token, timer-clothing-v1"}


@pytest.fixture
def shop_client(monkeypatch):
    theme, coat = _theme(), _timer_clothing()
    abs_item = _item(public_id="abs", slot="body", is_v2_only=True)
    hat = _item(public_id="hat", slot="hat")
    catalog = [theme, coat, abs_item, hat]
    rows = [_row(item.id, equipped_slot=item.slot if item in (theme, coat) else None)
            for item in catalog]

    async def session():
        yield FakeSession(get_obj=_CATALOG_PROFILE, exec_results=[catalog])

    async def user_rows(session, uid):
        return rows

    async def products(session, ids):
        return {item.id: item for item in catalog if item.id in ids}

    async def load(session, public_id):
        return next(item for item in catalog if item.public_id == public_id)

    monkeypatch.setattr(shop, "_user_rows", user_rows)
    monkeypatch.setattr(shop, "_products_by_ids", products)
    monkeypatch.setattr(shop, "_load_item", load)
    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_current_user] = lambda: UID
    try:
        yield TestClient(app), rows
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("token", [None, "", "timer-clothing-v10", "TIMER-CLOTHING-V1"])
def test_old_requests_hide_timer_items(shop_client, token):
    client, rows = shop_client
    headers = {} if token is None else {"X-Moly-Capabilities": token}
    for path, key in [("/v2/shop/products", "items"), ("/v2/inventory", "data")]:
        response = client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        products = response.json()[key]
        assert "raincoat" not in {p["id"] for p in products}
        assert "abs" in {p["id"] for p in products}
        assert all("timer" not in p["assets"] for p in products)
    for path, key in [("/shop/products", "items"), ("/inventory", "data")]:
        response = client.get(path, headers=CAPABLE)
        assert response.status_code == 200, response.text
        assert not {"raincoat", "abs"} & {p["id"] for p in response.json()[key]}
    assert rows[1].equipped_slot == "body"


def test_supported_assets_and_mixed_device_equipment(shop_client):
    client, rows = shop_client
    products = client.get("/v2/shop/products", headers=CAPABLE).json()["items"]
    coat = next(p for p in products if p["id"] == "raincoat")
    assert len(coat["assets"]["timer"]) == 3
    assert coat["assets"]["scene"] is None and coat["assets"]["detail_url"] is None
    body = dict(theme_id="theme_default", hat_id="hat", glasses_id=None, neck_id=None, body_id=None)
    assert client.get("/v2/inventory/equipment", headers=CAPABLE).json()["body_id"] == "raincoat"
    assert client.get("/v2/inventory/equipment").json()["body_id"] is None
    response = client.put("/v2/inventory/equipment", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["body_id"] is None
    assert rows[1].equipped_slot == "body" and rows[3].equipped_slot == "hat"
    # An explicit compatible replacement retains the existing full replacement semantics.
    response = client.put("/v2/inventory/equipment", json=body | {"body_id": "abs"})
    assert response.json()["body_id"] == "abs"
    assert rows[1].equipped_slot is None and rows[2].equipped_slot == "body"
    assert client.get("/inventory/equipment").json()["body_id"] is None
    legacy = dict(theme_id="theme_default", head_id="hat", neck_id=None, body_id=None)
    assert client.put("/inventory/equipment", json=legacy).status_code == 200
    assert rows[2].equipped_slot == "body"
    assert client.put("/v2/inventory/equipment", json=body, headers=CAPABLE).status_code == 200
    assert rows[2].equipped_slot is None


def test_purchase_header_reaches_service(shop_client):
    client, _ = shop_client
    response = client.post("/shop/purchases", json={"product_id": "raincoat"})
    assert response.status_code == 404
    response = client.post("/shop/purchases", json={"product_id": "raincoat"}, headers=CAPABLE)
    assert response.json()["error"]["code"] == "ALREADY_OWNED"


BUNDLED = {"X-Moly-Bundled-Themes": "theme_default, theme_onsen"}


def test_bundled_theme_needs_the_app_bundle(monkeypatch):
    home = _theme()
    onsen = _theme(public_id="theme_onsen", price_hay=4000, sort_order=3)
    onsen.assets = {**onsen.assets, "bundled": True}
    catalog = [home, onsen]
    rows = [_row(home.id), _row(onsen.id, equipped_slot="theme")]

    async def session():
        yield FakeSession(get_obj=_CATALOG_PROFILE, exec_results=[catalog])

    async def user_rows(session, uid):
        return rows

    async def products(session, ids):
        return {item.id: item for item in catalog if item.id in ids}

    async def load(session, public_id):
        return next(item for item in catalog if item.public_id == public_id)

    monkeypatch.setattr(shop, "_user_rows", user_rows)
    monkeypatch.setattr(shop, "_products_by_ids", products)
    monkeypatch.setattr(shop, "_load_item", load)
    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_current_user] = lambda: UID
    try:
        client = TestClient(app)
        for headers in ({}, CAPABLE, {"X-Moly-Bundled-Themes": "theme_default"}):
            themes = client.get("/v2/shop/products", headers=headers).json()["themes"]
            assert [p["id"] for p in themes] == ["theme_default"]
            owned = client.get("/v2/inventory", headers=headers).json()["data"]
            assert "theme_onsen" not in {p["id"] for p in owned}
            equipment = client.get("/v2/inventory/equipment", headers=headers).json()
            assert equipment["theme_id"] == "theme_default"
            response = client.post(
                "/shop/purchases", json={"product_id": "theme_onsen"}, headers=headers
            )
            assert response.status_code == 404
        assert client.get("/inventory/equipment").json()["theme_id"] == "theme_default"
        assert "theme_onsen" not in {p["id"] for p in client.get("/shop/products").json()["themes"]}

        themes = client.get("/v2/shop/products", headers=BUNDLED).json()["themes"]
        onsen_dto = next(p for p in themes if p["id"] == "theme_onsen")
        assert "bundled" not in onsen_dto["assets"]
        equipment = client.get("/v2/inventory/equipment", headers=BUNDLED).json()
        assert equipment["theme_id"] == "theme_onsen"
        response = client.post(
            "/shop/purchases", json={"product_id": "theme_onsen"}, headers=BUNDLED
        )
        assert response.json()["error"]["code"] == "ALREADY_OWNED"
    finally:
        app.dependency_overrides.clear()
