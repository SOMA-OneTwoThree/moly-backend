"""Mixed-version wallet history uses real serialization without a database connection."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.economy import router
from app.core.db import get_session
from app.core.errors import register_error_handlers
from app.core.security import get_current_user
from app.services import economy


UID = str(uuid.UUID("00000000-0000-4000-8000-000000000001"))
OLD_TYPES = (
    "attendance", "ad_reward", "routine_reward", "iap_purchase",
    "subscription_grant", "shop_purchase", "refund_revoke", "admin_adjustment",
)
NEW_TYPES = ("mood_reward", "training_reward", "article_reward")


class ReadSession:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.rows))


def mixed_rows():
    # The extra row establishes next_cursor without dropping any unfamiliar type.
    return [
        SimpleNamespace(
            id=201 - index, user_id=uuid.UUID(UID), type=kind,
            amount=-20 if kind == "shop_purchase" else 20,
            balance_after=-10 if index == 0 else 100 + index,
            created_at=datetime(2026, 10, 28, tzinfo=timezone.utc) - timedelta(seconds=index),
        )
        for index, kind in enumerate(OLD_TYPES + NEW_TYPES + ("attendance",))
    ]


def assert_page(page, rows, *, canonical, api=False):
    assert page["next_cursor"] == str(rows[10].id)
    assert len(page["data"]) == 11
    for item, row in zip(page["data"], rows[:11], strict=True):
        expected_type = row.type if canonical or row.type in OLD_TYPES else "admin_adjustment"
        assert item["type"] == expected_type
        assert item["id"] == str(row.id)
        assert item["amount"] == row.amount
        assert item["balance_after"] == (0 if row.balance_after < 0 else row.balance_after)
        if api:
            assert datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")) == row.created_at
        else:
            assert item["created_at"] == row.created_at.isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical", [False, True])
async def test_service_preserves_mixed_page_and_canonical_rows(canonical):
    rows = mixed_rows()
    originals = [vars(row).copy() for row in rows]
    session = ReadSession(rows)
    # Exercise the default argument as well as explicit opt-in.
    options = {"ledger_types_v2": True} if canonical else {}
    page = await economy.list_transactions(session, UID, limit=11, cursor="999", **options)
    assert_page(page, rows, canonical=canonical)
    assert [vars(row) for row in rows] == originals
    assert len(session.statements) == 1
    parameters = session.statements[0].compile().params
    assert uuid.UUID(UID) in parameters.values()
    assert 999 in parameters.values()
    assert 12 in parameters.values()  # Existing limit + one lookahead row.


@pytest.fixture
def wallet_client():
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(router)
    rows = mixed_rows()
    session = ReadSession(rows)

    async def session_dependency():
        yield session

    app.dependency_overrides[get_session] = session_dependency
    app.dependency_overrides[get_current_user] = lambda: UID
    return TestClient(app), app, session, rows


@pytest.mark.parametrize("token,canonical", [
    (None, False),
    ("", False),
    ("future-token", False),
    ("wallet-ledger-types-v20", False),
    ("prefix-wallet-ledger-types-v2", False),
    ("WALLET-LEDGER-TYPES-V2", False),
    ("wallet-ledger-types-v2;future-token", False),
    ("wallet-ledger-types-v2", True),
    ("future-token, wallet-ledger-types-v2 , diary-reference-v1", True),
])
def test_api_capability_preserves_page_metadata_and_identity(wallet_client, token, canonical):
    client, _, session, rows = wallet_client
    originals = [vars(row).copy() for row in rows]
    headers = {} if token is None else {"X-Moly-Capabilities": token}
    response = client.get("/wallet/transactions?limit=11&cursor=999", headers=headers)
    assert response.status_code == 200, response.text
    assert_page(response.json(), rows, canonical=canonical, api=True)
    assert [vars(row) for row in rows] == originals
    assert len(session.statements) == 1
    assert uuid.UUID(UID) in session.statements[0].compile().params.values()


def test_capability_does_not_bypass_authentication(wallet_client):
    client, app, session, _ = wallet_client
    del app.dependency_overrides[get_current_user]
    response = client.get(
        "/wallet/transactions", headers={"X-Moly-Capabilities": "wallet-ledger-types-v2"},
    )
    assert response.status_code == 401
    assert session.statements == []
