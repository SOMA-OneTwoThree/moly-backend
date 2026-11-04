"""New pipeline read origins only; existing DEV and complete outer rollback."""
import json
import uuid

import pytest
from sqlalchemy import text

from app.services import context_compat, context_retention, memory_pipeline
from worker import mem0_jobs
from tests.integration.test_checkpoint_continuity_dev import checkpoint_db as checkpoint_db


async def source(session,uid,seq,sender="user"):
    return (await session.execute(text("""INSERT INTO messages(user_id,sender,kind,content,
      activity_date,turn_seq,turn_position) VALUES(:u,:sender,'normal','Synthetic origin source',
      CURRENT_DATE,:seq,:pos) RETURNING id"""),
      {"u":uid,"sender":sender,"seq":seq,"pos":(1 if sender=="user" else 2) if seq else None})).scalar_one()


async def prefix_suffix(session,uid):
    unsequenced = await source(session,uid,None)
    await source(session,uid,1)
    boundary = await source(session,uid,1,"moly")
    suffix = [await source(session,uid,2),await source(session,uid,2,"moly")]
    return unsequenced,boundary,suffix


async def test_actual_new_origin_skips_only_old_prefix_and_preserves_unsequenced_rows(checkpoint_db):
    session,(uid,other)=checkpoint_db
    old,boundary,suffix = await prefix_suffix(session,uid)
    await source(session,other,99)
    assert await memory_pipeline.enroll(session,uid,source_after_message_id=boundary,privacy_epoch=0)
    state = await memory_pipeline.load(session,uid)
    assert (state.source_through_turn_seq,state.ingest_through_turn_seq,state.consolidated_through_turn_seq)==(1,1,1)
    assert state.historical_upper_turn_seq == 0 and state.accepts_live_ingest
    await memory_pipeline.advance_source(session,uid,turn_seq=2)
    assert await memory_pipeline.next_ingest_turn(session,uid,cursor=state.ingest_through_turn_seq)==2
    assert await mem0_jobs.resolve_chunk(session,user_id=uid,from_seq=1,upper=2,idle_s=180,max_turns=12)==(2,2)
    rows=(await session.execute(mem0_jobs._SOURCE_MESSAGES,{"user_id":uid,"from_seq":1,"to_seq":2})).all()
    assert [row[0] for row in rows]==suffix
    assert (await session.execute(text("SELECT turn_seq FROM messages WHERE user_id=:u AND id=:i"),{"u":uid,"i":old})).scalar_one() is None
    b=context_compat.Baseline(str(uuid.uuid4()),str(uid),0,0,boundary,boundary,None,0,None,None,None,0,None,None)
    await session.execute(text("UPDATE chat_contexts SET retention_compat_json=CAST(:b AS jsonb) WHERE user_id=:u"),
                          {"u":uid,"b":json.dumps(b.payload())})
    # Old NULL sequence is outside the new processing range; a suffix NULL is not.
    row=(await session.execute(context_retention._COVERAGE,{"user_id":uid,"generation":0,
        "through_id":suffix[-1],"after_id":boundary})).first()
    assert row[8]==0
    latest=await source(session,uid,None)
    row=(await session.execute(context_retention._COVERAGE,{"user_id":uid,"generation":0,
        "through_id":latest,"after_id":boundary})).first()
    assert row[8]==1
    assert not await context_retention.processing_complete(session,user_id=uid,generation=0,through_id=latest)


async def test_actual_existing_ready_pipeline_is_never_repositioned(checkpoint_db):
    session,(uid,_)=checkpoint_db
    _,boundary,_ = await prefix_suffix(session,uid)
    assert await memory_pipeline.enroll(session,uid)
    before=await memory_pipeline.load(session,uid)
    assert not await memory_pipeline.enroll(session,uid,source_after_message_id=boundary,privacy_epoch=3)
    assert await memory_pipeline.load(session,uid)==before
    await memory_pipeline.advance_source(session,uid,turn_seq=2)
    assert await memory_pipeline.next_ingest_turn(session,uid,cursor=0)==1


async def test_actual_crossed_normal_turn_is_rejected_without_pipeline_creation(checkpoint_db):
    session,(uid,_)=checkpoint_db
    boundary=await source(session,uid,1)
    await source(session,uid,1,"moly")
    with pytest.raises(memory_pipeline.EnrollmentOriginError,match="crossed_turn"):
        await memory_pipeline.enroll(session,uid,source_after_message_id=boundary)
    assert not (await memory_pipeline.load(session,uid)).exists
