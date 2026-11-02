"""Existing development Supabase only; all synthetic changes roll back."""
from sqlalchemy import text

from app.models.chat_context import ChatContext
from app.services import checkpoint, checkpoint_repo, context_retention

pytest_plugins = ["tests.integration.test_checkpoint_continuity_dev"]


async def source(session, uid, *, kind="normal"):
    return (await session.execute(text("""
      INSERT INTO messages(user_id,sender,kind,content,activity_date,turn_seq,turn_position)
      VALUES(:u,'user',:kind,'Synthetic continuity source',CURRENT_DATE,1,1)
      RETURNING id
    """), {"u": uid, "kind": kind})).scalar_one()


async def test_v5_adoption_is_persistent_and_generation_scoped(checkpoint_db):
    session, (uid, other) = checkpoint_db
    through = await source(session, uid)
    original = await session.get(ChatContext, uid, populate_existing=True)
    assert original.retention_v5_adopted is False
    identity = await checkpoint_repo.insert(
        session, user_id=uid, through_message_id=through, summary="",
        source_hash="a" * 64, expected_generation=0,
        version=checkpoint.RETENTION_SUMMARIZER_VERSION,
        state_json=context_retention.empty_state(), validation_json={}, coverage_from=through,
    )
    assert identity is not None
    adopted = await session.get(ChatContext, uid, populate_existing=True)
    assert adopted.retention_v5_adopted is True
    unrelated = await session.get(ChatContext, other, populate_existing=True)
    assert unrelated.retention_v5_adopted is False
    await session.execute(text("UPDATE chat_contexts SET memory_generation=1 WHERE user_id=:u"), {"u": uid})
    changed = await session.get(ChatContext, uid, populate_existing=True)
    assert changed.retention_v5_adopted is False


async def test_normal_coverage_requires_both_fact_and_contract_processing(checkpoint_db):
    session, (uid, _) = checkpoint_db
    through = await source(session, uid)
    assert not await context_retention.processing_complete(session, user_id=uid, generation=0, through_id=through)
    await session.execute(text("""
      INSERT INTO memory_pipeline_states(user_id,mode,bootstrap_status,source_through_turn_seq,
                                         ingest_through_turn_seq,consolidated_through_turn_seq)
      VALUES(:u,'v2','ready',1,1,1)
      ON CONFLICT(user_id) DO UPDATE SET mode='v2',bootstrap_status='ready',
        source_through_turn_seq=1,ingest_through_turn_seq=1,consolidated_through_turn_seq=1
    """), {"u": uid})
    assert not await context_retention.processing_complete(session, user_id=uid, generation=0, through_id=through)
    await session.execute(text("""
      UPDATE chat_contexts SET contract_generation=0,contract_processed_through_message_id=:through
      WHERE user_id=:u
    """), {"u": uid, "through": through})
    assert await context_retention.processing_complete(session, user_id=uid, generation=0, through_id=through)


async def test_safety_only_range_is_policy_excluded_without_fact_extraction(checkpoint_db):
    session, (uid, _) = checkpoint_db
    through = await source(session, uid, kind="safety")
    assert await context_retention.processing_complete(session, user_id=uid, generation=0, through_id=through)
