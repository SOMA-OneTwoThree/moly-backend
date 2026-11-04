"""Read-state invalidation in the existing development DB; outer rollback only."""
import hashlib
import uuid

from sqlalchemy import text

from app.services import checkpoint, checkpoint_repo, context_compat as compat
from tests.integration.test_checkpoint_continuity_dev import checkpoint_db as checkpoint_db


async def test_legacy_prose_is_quarantined_without_rebinding_sources_or_rewriting_records(checkpoint_db):
    session, (uid, _) = checkpoint_db
    source = await session.scalar(text("""INSERT INTO messages(user_id,sender,kind,content,activity_date)
      VALUES(:u,'user','normal','Synthetic existing source',CURRENT_DATE) RETURNING id"""), {'u':uid})
    cp_id = await checkpoint_repo.insert(session,user_id=uid,through_message_id=source,
        summary='Synthetic opaque old summary',source_hash='a'*64,expected_generation=0,
        version=checkpoint.LEGACY_SUMMARIZER_VERSION)
    cp = checkpoint.Checkpoint(cp_id,source,'Synthetic opaque old summary',checkpoint.LEGACY_SUMMARIZER_VERSION,'a'*64)
    value = await compat.initialize(session,user_id=uid,generation=0,privacy_epoch=0,
        source_after=source,legacy_anchor=source+1,legacy_checkpoint=cp,locale='ko')
    assert await compat.legacy_summary(session,value)==cp.summary
    assert await compat.matches_current(session,value)
    async with session.begin_nested() as savepoint:
        await compat.invalidate_legacy_summary(session,value,revision=4)
        assert await compat.legacy_summary(session,value)==''
        assert await compat.matches_current(session,value)
        await savepoint.rollback()
    assert await compat.legacy_summary(session,value)==cp.summary
    await compat.invalidate_legacy_summary(session,value,revision=4)
    await compat.invalidate_legacy_summary(session,value,revision=9)
    row=(await session.execute(text("""SELECT retention_compat_json,
      retention_compat_summary_invalidated_revision FROM chat_contexts WHERE user_id=:u"""),{'u':uid})).one()
    assert row[0]==value.payload() and row[1]==4
    original=(await session.execute(text("SELECT summary,source_hash FROM conversation_checkpoints WHERE id=:id"),{'id':cp_id})).one()
    assert original[0]==cp.summary and hashlib.sha256(original[0].encode()).hexdigest()==value.legacy_summary_hash
    assert original[1]==cp.source_hash
    assert await session.scalar(text('SELECT content FROM messages WHERE id=:id'),{'id':source})=='Synthetic existing source'
    assert await session.scalar(text('SELECT count(*) FROM async_jobs WHERE user_id=:u'),{'u':uid})==0


async def test_nullable_reader_columns_have_no_defaults_or_implicit_backfill(checkpoint_db):
    session,(uid,_)=checkpoint_db
    row=(await session.execute(text("""SELECT retention_compat_json,
      retention_compat_summary_invalidated_revision FROM chat_contexts WHERE user_id=:u"""),{'u':uid})).one()
    assert tuple(row)==(None,None)
    rows=(await session.execute(text("""SELECT table_name,column_name,data_type,is_nullable,column_default
      FROM information_schema.columns WHERE table_schema='public' AND (table_name,column_name) IN
      (('chat_contexts','retention_compat_json'),('chat_contexts','retention_compat_summary_invalidated_revision'),
       ('user_interaction_contracts','validation_json'))"""))).all()
    assert len(rows)==3
    assert all(row[3]=='YES' and row[4] is None for row in rows)
    assert {row[2] for row in rows}=={'jsonb','bigint'}


async def test_semantic_retirement_and_legacy_invalidation_are_one_transaction(checkpoint_db):
    from app.services import mem0_registry_repo as repo
    from app.services.mem0_consolidation import Transition
    session,(uid,_)=checkpoint_db
    value=await compat.initialize(session,user_id=uid,generation=0,privacy_epoch=0,
        source_after=0,legacy_anchor=0,legacy_checkpoint=None,locale='ko')
    await session.execute(text("""INSERT INTO memory_pipeline_states(user_id,mode,bootstrap_status)
      VALUES(:u,'v2','ready')"""),{'u':uid})
    identity=uuid.uuid4()
    await session.execute(text("""INSERT INTO mem0_memory_registry(id,user_id,provider,
      collection_version,provider_memory_id,source_turn_seq,content_hash,canonical_text,
      semantic_status,provider_delete_state,schema_version)
      VALUES(:id,:u,'openai','v2',:provider,1,:hash,'Synthetic old fact','active','kept','v1')"""),
      {'id':identity,'u':uid,'provider':uuid.uuid4(),'hash':'a'*64})
    async with session.begin_nested() as savepoint:
        assert await repo.apply_transitions(session,uid,[Transition(registry_id=identity,
            semantic_status='excluded')],expected_revision=0,classification_version='synthetic')==1
        assert await session.scalar(text('SELECT retention_compat_summary_invalidated_revision FROM chat_contexts WHERE user_id=:u'),{'u':uid})==1
        assert await compat.matches_current(session,value)
        await savepoint.rollback()
    assert await session.scalar(text('SELECT retention_compat_summary_invalidated_revision FROM chat_contexts WHERE user_id=:u'),{'u':uid}) is None
    assert await session.scalar(text('SELECT semantic_status FROM mem0_memory_registry WHERE id=:id'),{'id':identity})=='active'
