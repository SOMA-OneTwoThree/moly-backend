"""Global activation on the existing development DB; every change rolls back."""
from sqlalchemy import text

from app.services import checkpoint, checkpoint_repo, context_retention
from tests.integration.test_checkpoint_continuity_dev import checkpoint_db as checkpoint_db


async def set_global(session, enabled):
    await session.execute(text("""INSERT INTO app_config(key,value,updated_at)
      VALUES('context_retention_v2_enabled',CAST(:value AS jsonb),now())
      ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at"""),
      {"value": "true" if enabled else "false"})


async def test_global_activation_includes_both_existing_accounts_without_lists(checkpoint_db):
    session, users = checkpoint_db
    await set_global(session, True)
    for uid in users:
        assert await context_retention.adopted_user(session, user_id=uid)
        assert await context_retention.adopted_user(session, user_id=uid, lock=True)


async def test_global_off_preserves_written_format_and_leaves_other_account_legacy(checkpoint_db):
    session, (mine, other) = checkpoint_db
    await set_global(session, False)
    mid = (await session.execute(text("""INSERT INTO messages
      (user_id,sender,kind,content,activity_date,turn_seq,turn_position)
      VALUES(:u,'user','normal','Synthetic source',CURRENT_DATE,1,1) RETURNING id"""),
      {"u": mine})).scalar_one()
    await checkpoint_repo.insert(session, user_id=mine, through_message_id=mid,
        summary="", source_hash="a" * 64, expected_generation=0,
        version=checkpoint.RETENTION_SUMMARIZER_VERSION,
        state_json=context_retention.empty_state(), validation_json={}, coverage_from=mid)
    assert await context_retention.adopted_user(session, user_id=mine)
    assert not await context_retention.adopted_user(session, user_id=other)
    assert not await context_retention.enabled_now(session)
