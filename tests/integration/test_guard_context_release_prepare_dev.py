"""Existing development DB only: owned synthetic migration transactions roll back."""
from sqlalchemy import text

from scripts import prepare_guard_context_release as release
from tests.integration.test_checkpoint_continuity_dev import checkpoint_db as checkpoint_db
from tests.integration.test_provided_memory_dev import seed_memory, CANONICAL
from tests.integration.test_global_guard_retention_dev import set_global


async def seed_release(session, uid):
    rid, mid = await seed_memory(session, uid)
    await session.execute(text("""INSERT INTO messages(user_id,sender,kind,content,activity_date,
      turn_seq,turn_position) VALUES(:u,'moly','normal','Synthetic reply',CURRENT_DATE,1,2)"""), {'u': uid})
    await session.execute(text("""UPDATE chat_contexts SET last_committed_turn_seq=1
      WHERE user_id=:u"""), {'u': uid})
    await session.execute(text("""INSERT INTO mem0_ingest_candidates(user_id,turn_seq,candidate_hash,
      schema_version,extractor_version,normalizer_version,provider_memory_id,candidate_text,status)
      SELECT user_id,source_turn_seq,content_hash,'mem0-candidate-v1','synthetic',
      'mem0-normalizer-v1',provider_memory_id,:body,'committed'
      FROM mem0_memory_registry WHERE id=:r"""), {'r': rid, 'body': CANONICAL})
    await session.execute(text("UPDATE mem0_memory_registry SET canonical_text=NULL WHERE id=:r"), {'r': rid})
    await set_global(session, True)
    row = (await session.execute(text(str(release._USERS).replace('ORDER BY p.id', 'WHERE p.id=:uid')),
                                {'uid': uid})).mappings().one()
    return rid, mid, await release.read_user(session, dict(row))


async def test_prepare_restores_proved_canonical_and_reserves_without_editing_sources(checkpoint_db):
    session, (uid, _) = checkpoint_db
    rid, _, plan = await seed_release(session, uid)
    before = (await session.execute(text('SELECT id,content,kind,turn_seq FROM messages WHERE user_id=:u ORDER BY id'),
                                    {'u': uid})).all()
    await release.prepare_user(session, plan)
    assert await session.scalar(text('SELECT canonical_text FROM mem0_memory_registry WHERE id=:r'), {'r': rid}) == CANONICAL
    assert (await session.execute(text('SELECT id,content,kind,turn_seq FROM messages WHERE user_id=:u ORDER BY id'),
                                  {'u': uid})).all() == before
    jobs = await session.scalar(text('SELECT count(*) FROM async_jobs WHERE user_id=:u'), {'u': uid})
    assert jobs > 0
    await release.prepare_user(session, plan)
    assert await session.scalar(text('SELECT count(*) FROM async_jobs WHERE user_id=:u'), {'u': uid}) == jobs
    assert not await release.audit_user(session, plan)


async def test_prepare_rejects_changed_raw_without_filling_canonical(checkpoint_db):
    import pytest

    session, (uid, _) = checkpoint_db
    rid, mid, plan = await seed_release(session, uid)
    await session.execute(text("UPDATE messages SET content=content||' Changed.' WHERE id=:m"), {'m': mid})
    with pytest.raises(ValueError):
        await release.prepare_user(session, plan)
    assert await session.scalar(text('SELECT canonical_text FROM mem0_memory_registry WHERE id=:r'), {'r': rid}) is None
