"""Automatic development adoption requires the exact compatible fleet and keys."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import activate_chat_content_crypto_dev as gate

PROOF = {"capability": "chat-content-aead-v1", "environment": "development", "version": "a" * 40,
         "started": False, "format_version": 1, "active_key_id": 1,
         "key_fingerprints": {"1": "b" * 64}, "schema": "ok", "self_test": "ok"}


@pytest.mark.parametrize("field,bad", [("version", "c" * 40), ("environment", "production"),
    ("started", None), ("active_key_id", 2), ("key_fingerprints", {"1": "d" * 64}),
    ("schema", "missing"), ("self_test", "failed"), ("capability", "legacy")])
def test_other_reader_or_key_proof_cannot_adopt(field, bad):
    proof = {"api": deepcopy(PROOF), "consumer": {**deepcopy(PROOF), field: bad}}
    with pytest.raises(ValueError):
        gate.validate_proofs(proof, PROOF)


def test_both_roles_required_and_idempotent_adoption_allowed():
    with pytest.raises(ValueError):
        gate.validate_proofs({"api": PROOF}, PROOF)
    gate.validate_proofs({"api": PROOF, "consumer": PROOF}, {**PROOF, "started": True})


async def test_production_can_never_run_development_activation(monkeypatch):
    monkeypatch.setattr(gate.settings, "environment", "production")
    check = AsyncMock()
    monkeypatch.setattr(gate, "check", check)
    with pytest.raises(ValueError, match="development_target_required"):
        await gate.run("a" * 40, {})
    check.assert_not_awaited()


async def test_commit_happens_only_after_current_selftest_and_matching_fleet(monkeypatch):
    monkeypatch.setattr(gate.settings, "environment", "development")
    monkeypatch.setattr(gate.settings, "supabase_db_connection_string",
                        "postgresql://postgres.wywzjslvxwttxkecbyis:synthetic@localhost/postgres")
    monkeypatch.setattr(gate, "check", AsyncMock(return_value=PROOF))
    session = SimpleNamespace(commit=AsyncMock())

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(gate, "get_sessionmaker", lambda: Context)
    write = AsyncMock(return_value=SimpleNamespace(started=True))
    monkeypatch.setattr(gate, "activate", write)
    with pytest.raises(ValueError):
        await gate.run("a" * 40, {"api": PROOF})
    write.assert_not_awaited()
    session.commit.assert_not_awaited()
    output = await gate.run("a" * 40, {"api": PROOF, "consumer": PROOF})
    assert output["started"] is True
    write.assert_awaited_once_with(session, expected_key_id=1, expected_required_key_ids=(1,), release_sha="a" * 40)
    session.commit.assert_awaited_once()


def test_runtime_activation_dependencies_are_packaged():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    assert "db/envfile.py ./db/" in source
    copied_scripts = {
        item for line in source.splitlines() if line.startswith("COPY ") and line.endswith(" ./scripts/")
        for item in line.split()[1:-1]
    }
    assert {
        "scripts/activate_chat_content_crypto_dev.py",
        "scripts/activate_chat_content_crypto.py",
        "scripts/preflight_chat_content_encryption.py",
        "scripts/check_chat_content_crypto.py",
    } <= copied_scripts
    assert "COPY db/changes/chat_content_encryption.sql ./db/changes/" in source
    assert "scripts/check_chat_content_crypto.py" in source
