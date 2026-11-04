"""Legacy metadata capture never rewrites or certifies existing content."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.models.chat_context import ChatContext
from app.services import context_compat as compat, checkpoint

UID = uuid.UUID(int=41)


def baseline(**changes):
    values = dict(baseline_id=str(uuid.UUID(int=40)), user_id=str(UID), generation=2,
        privacy_epoch=3, source_after=100, legacy_anchor=70,
        legacy_checkpoint_id=str(uuid.UUID(int=42)), legacy_checkpoint_through=100,
        legacy_checkpoint_hash="a"*64, legacy_checkpoint_version=checkpoint.LEGACY_SUMMARIZER_VERSION,
        legacy_summary_hash=compat.hashlib.sha256(b"old summary").hexdigest(),
        legacy_contract_after=80, legacy_contract_id=str(uuid.UUID(int=43)), legacy_contract_hash="b"*64)
    return compat.Baseline(**{**values, **changes})


def test_metadata_roundtrip_preserves_opaque_identities_and_does_not_contain_old_text():
    value = baseline()
    assert compat.Baseline.parse(value.payload()) == value
    assert value.fingerprint == compat.Baseline.parse(dict(reversed(list(value.payload().items())))).fingerprint
    assert "old summary" not in str(value.payload())
    assert replace(value, source_after=101).fingerprint != value.fingerprint
    assert replace(value, generation=3).fingerprint != value.fingerprint


@pytest.mark.parametrize("field,value", [("generation", True), ("privacy_epoch", -1),
    ("source_after", "100"), ("source_after", 99), ("legacy_checkpoint_hash", None),
    ("legacy_checkpoint_version", checkpoint.RETENTION_SUMMARIZER_VERSION),
    ("legacy_contract_hash", None), ("user_id", "other-user")])
def test_unproved_or_forged_legacy_boundaries_are_rejected(field, value):
    with pytest.raises(compat.CompatibilityError):
        baseline(**{field: value})


async def test_old_or_forgotten_generations_are_not_recaptured_by_read():
    value = baseline()
    row = ChatContext(user_id=UID, memory_generation=2, retention_compat_json=value.payload())
    session = SimpleNamespace(get=AsyncMock(return_value=row))
    assert await compat.load(session, UID, 2) == value
    assert await compat.load(session, UID, 3) is None
    row.retention_compat_json = None
    assert await compat.load(session, UID, 2) is None
    assert not hasattr(session, "execute")


async def test_foreign_baseline_cannot_be_read_through_another_subject():
    row = ChatContext(user_id=UID, memory_generation=2,
        retention_compat_json=baseline(user_id=str(uuid.UUID(int=99))).payload())
    with pytest.raises(compat.CompatibilityError, match="compat_owner"):
        await compat.load(SimpleNamespace(get=AsyncMock(return_value=row)), UID, 2)


async def test_capture_records_only_metadata_with_generation_and_old_value_cas():
    row = ChatContext(user_id=UID, memory_generation=2, anchor_message_id=70,
        contract_generation=2, contract_processed_through_message_id=80)
    document = [{"kind": "response_style", "target_tag": "brief"}]
    result = SimpleNamespace(first=lambda: (uuid.UUID(int=43), document))
    session = SimpleNamespace(get=AsyncMock(return_value=row),
        execute=AsyncMock(side_effect=[result, SimpleNamespace(first=lambda: ({},))]))
    old = checkpoint.Checkpoint(uuid.UUID(int=42), 100, "old summary",
        checkpoint.LEGACY_SUMMARIZER_VERSION, "a"*64)
    made = await compat.initialize(session, user_id=UID, generation=2, privacy_epoch=3,
        source_after=100, legacy_anchor=70, legacy_checkpoint=old, locale="en")
    assert made.legacy_contract_hash == compat.digest(document)
    assert made.legacy_summary_hash == compat.hashlib.sha256(old.summary.encode()).hexdigest()
    assert row.retention_compat_json == made.payload()
    assert row.anchor_message_id == 70
    sql = "\n".join(str(call.args[0]) for call in session.execute.await_args_list)
    assert "UPDATE chat_contexts" in sql and "memory_generation=:generation" in sql
    assert "IS NOT DISTINCT FROM CAST(:previous AS jsonb)" in sql
    assert all(word not in sql for word in ("UPDATE messages", "UPDATE conversation_checkpoints",
        "UPDATE user_interaction_contracts", "INSERT INTO async_jobs"))
    assert not hasattr(session, "commit")


async def test_existing_baseline_is_immutable_and_requires_same_privacy_epoch():
    row = ChatContext(user_id=UID, memory_generation=2, retention_compat_json=baseline().payload())
    session = SimpleNamespace(get=AsyncMock(return_value=row))
    args = dict(user_id=UID, generation=2, privacy_epoch=3, source_after=1000,
        legacy_anchor=999, legacy_checkpoint=None, locale="en")
    assert await compat.initialize(session, **args) == baseline()
    with pytest.raises(compat.CompatibilityError, match="compat_privacy_epoch"):
        await compat.initialize(session, **{**args, "privacy_epoch": 4})
    assert not hasattr(session, "execute")


async def test_new_generation_never_recaptures_unversioned_old_contract():
    row = ChatContext(user_id=UID, memory_generation=3, retention_compat_json=baseline().payload(),
        contract_generation=2, contract_processed_through_message_id=80)
    session = SimpleNamespace(get=AsyncMock(return_value=row),
        execute=AsyncMock(return_value=SimpleNamespace(first=lambda: ({},))))
    made = await compat.initialize(session, user_id=UID, generation=3, privacy_epoch=4,
        source_after=150, legacy_anchor=151, legacy_checkpoint=None, locale='en')
    assert made.legacy_contract_id is None and made.legacy_contract_after == 0
    assert made.generation == 3 and made.privacy_epoch == 4
    assert session.execute.await_count == 1
    assert 'UPDATE chat_contexts' in str(session.execute.await_args.args[0])


async def test_final_binding_check_uses_fresh_sql_not_cached_model():
    session = SimpleNamespace(scalar=AsyncMock(side_effect=[True, False]))
    assert await compat.matches_current(session, baseline())
    assert not await compat.matches_current(session, baseline())
    sql = str(session.scalar.await_args.args[0])
    assert 'memory_generation=:generation' in sql and 'retention_compat_json=CAST(:binding AS jsonb)' in sql


async def test_summary_invalidation_is_monotone_separate_from_immutable_binding():
    value = baseline()
    session = SimpleNamespace(scalar=AsyncMock(return_value=UID))
    await compat.invalidate_legacy_summary(session, value, revision=4)
    sql, params = session.scalar.await_args.args
    assert 'COALESCE(' in str(sql) and 'RETURNING user_id' in str(sql)
    assert 'SET retention_compat_json' not in str(sql)
    assert params['revision'] == 4 and params['generation'] == value.generation
    session.scalar.return_value = None
    with pytest.raises(compat.CompatibilityError, match='compat_changed'):
        await compat.invalidate_legacy_summary(session, value, revision=5)


@pytest.mark.parametrize("mutation", ["hash", "text", "generation_deleted"])
async def test_legacy_summary_is_read_only_and_cannot_silently_change(mutation):
    value = baseline()
    row = ["old summary", "a"*64, 100, checkpoint.LEGACY_SUMMARIZER_VERSION]
    if mutation == "hash":
        row[1] = "c"*64
    elif mutation == "text":
        row[0] = "changed old summary"
    result = None if mutation == "generation_deleted" else tuple(row)
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(first=lambda: result)))
    if result is None:
        assert await compat.legacy_summary(session, value) == ""
    else:
        with pytest.raises(compat.CompatibilityError, match="compat_legacy_summary_changed"):
            await compat.legacy_summary(session, value)
    assert not hasattr(session, "commit")
