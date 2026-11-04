"""Existing development DB; synthetic proof changes are always rolled back."""

import hashlib
import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.services import provided_memory as provided
from app.services.mem0_ingest import candidate_hash
from tests.integration.test_checkpoint_continuity_dev import checkpoint_db as checkpoint_db

RAW = "Verified source. Additional unchanged text."
QUOTE = "Verified source."
CANONICAL = "Synthetic verified memory."


async def seed_memory(session, uid):
    await session.execute(
        text("""INSERT INTO memory_pipeline_states(user_id,mode,bootstrap_status,
      source_through_turn_seq,ingest_through_turn_seq,consolidated_through_turn_seq)
      VALUES(:u,'v2','ready',1,1,1) ON CONFLICT(user_id) DO UPDATE SET
      mode='v2',bootstrap_status='ready',source_through_turn_seq=1,
      ingest_through_turn_seq=1,consolidated_through_turn_seq=1"""),
        {"u": uid},
    )
    mid = (
        await session.execute(
            text("""INSERT INTO messages(user_id,sender,kind,content,
      activity_date,turn_seq,turn_position) VALUES(:u,'user','normal',:body,CURRENT_DATE,1,1)
      RETURNING id"""),
            {"u": uid, "body": RAW},
        )
    ).scalar_one()
    rid, pid = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text("""INSERT INTO mem0_memory_registry(id,user_id,provider,
      collection_version,provider_memory_id,source_turn_seq,content_hash,canonical_text,
      semantic_status,provider_delete_state,schema_version)
      VALUES(:r,:u,'openai','v2',:pid,1,:hash,:body,'active','kept','v1')"""),
        {"r": rid, "u": uid, "pid": pid, "hash": candidate_hash(CANONICAL), "body": CANONICAL},
    )
    await session.execute(
        text("""INSERT INTO mem0_memory_sources(registry_id,user_id,
      source_turn_seq,source_message_id,source_sender,evidence_start_utf8,evidence_end_utf8,
      source_content_hash,source_occurred_at,source_activity_date,authority,extractor_version)
      VALUES(:r,:u,1,:mid,'user',0,:end,:hash,now(),CURRENT_DATE,'explicit_user','synthetic')"""),
        {
            "r": rid,
            "u": uid,
            "mid": mid,
            "end": len(QUOTE.encode()),
            "hash": hashlib.sha256(QUOTE.encode()).hexdigest(),
        },
    )
    return rid, mid


async def proof(session, uid, rid):
    return await provided.capture(
        session,
        user_id=uid,
        generation=0,
        ids=(rid,),
        language="en",
        today=date.today(),
        tz_name="UTC",
    )


async def test_optional_proof_survives_bookkeeping_and_unrelated_new_memory(checkpoint_db):
    session, (uid, other) = checkpoint_db
    rid, _ = await seed_memory(session, uid)
    snapshots = await proof(session, uid, rid)
    await session.execute(
        text("""UPDATE mem0_memory_registry SET revision=revision+1,
      classification_version='synthetic-update',context_priority='required',updated_at=now()
      WHERE id=:r"""),
        {"r": rid},
    )
    await seed_memory(session, other)
    assert await provided.validate(session, user_id=uid, generation=0, snapshots=snapshots)


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE mem0_memory_registry SET canonical_text='changed',content_hash='changed' WHERE id=:r",
        "UPDATE mem0_memory_registry SET semantic_status='duplicate' WHERE id=:r",
        "UPDATE mem0_memory_registry SET semantic_status='superseded' WHERE id=:r",
        "UPDATE mem0_memory_registry SET provider_delete_state='pending' WHERE id=:r",
        "UPDATE mem0_memory_registry SET collection_version='v3' WHERE id=:r",
        "UPDATE mem0_memory_registry SET provider_memory_id=gen_random_uuid() WHERE id=:r",
        "UPDATE mem0_memory_registry SET provider='other' WHERE id=:r",
        "UPDATE mem0_memory_sources SET authority='confirmed_user' WHERE registry_id=:r",
        "DELETE FROM mem0_memory_sources WHERE registry_id=:r",
        "UPDATE messages SET content=content||' Changed outside quote.' WHERE id=:mid",
        "UPDATE messages SET kind='safety' WHERE id=:mid",
        "UPDATE chat_contexts SET memory_generation=1 WHERE user_id=:u",
        "UPDATE memory_pipeline_states SET bootstrap_status='legacy' WHERE user_id=:u",
        "UPDATE mem0_memory_registry SET event_started_at=now()-interval '10 days' WHERE id=:r",
    ],
)
async def test_actual_provided_identity_content_source_or_time_change_blocks(
    checkpoint_db, mutation
):
    session, (uid, _) = checkpoint_db
    rid, mid = await seed_memory(session, uid)
    snapshots = await proof(session, uid, rid)
    await session.execute(text(mutation), {"r": rid, "mid": mid, "u": uid})
    assert not await provided.validate(session, user_id=uid, generation=0, snapshots=snapshots)


async def test_actual_source_sender_is_already_protected_by_database_foreign_key(checkpoint_db):
    session, (uid, _) = checkpoint_db
    rid, mid = await seed_memory(session, uid)
    snapshots = await proof(session, uid, rid)
    with pytest.raises(IntegrityError) as error:
        async with session.begin_nested():
            await session.execute(
                text("UPDATE messages SET sender='moly',turn_position=2 WHERE id=:mid"),
                {"mid": mid},
            )
    assert error.value.orig.sqlstate == "23503"
    assert await provided.validate(session, user_id=uid, generation=0, snapshots=snapshots)
