"""Existing dev DB, synthetic owners only; source clocks and writer commit order.

The imported fixture commits only its owned inert setup, creates no ready jobs,
then removes it. These tests never commit a global activation or invoke a model.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from tests.integration.test_online_retention_promotion_dev import (
    online_promotion_db as online_promotion_db, checked, require,
)


async def revision(session, f):
    return await session.scalar(text('SELECT r.revision+(SELECT count(*) FROM retention_preparation_revision_events e '
        'WHERE e.preparation_id=r.preparation_id AND e.user_id=r.user_id) '
        'FROM retention_preparation_revisions r WHERE r.preparation_id=:p AND r.user_id=:u'), {'p': f.identity, 'u': f.uid})


async def mutate(session, f, sql):
    await session.execute(text(sql), {'u': f.uid, 'm': f.mid, 'p': f.identity, 'c': f.staged})


_MUTATIONS = [
    ('raw_body', "UPDATE messages SET content=content||' Synthetic change.' WHERE id=:m"),
    ('raw_kind', "UPDATE messages SET kind='safety' WHERE id=:m"),
    ('raw_time', "UPDATE messages SET created_at=created_at+interval '1 second' WHERE id=:m"),
    ('raw_activity_date', "UPDATE messages SET activity_date=activity_date+1 WHERE id=:m"),
    ('profile_language', "UPDATE profiles SET language='ja' WHERE id=:u"),
    ('profile_timezone', "UPDATE profiles SET timezone='Asia/Tokyo' WHERE id=:u"),
    ('context_generation', 'UPDATE chat_contexts SET memory_generation=memory_generation+1 WHERE user_id=:u'),
    ('privacy_epoch', 'UPDATE privacy_subject_barriers SET epoch=epoch+1 WHERE user_id=:u'),
    ('privacy_state', "UPDATE privacy_subject_barriers SET state='deleting',operation_id=gen_random_uuid() WHERE user_id=:u"),
    ('pipeline_mode', "UPDATE memory_pipeline_states SET mode='legacy' WHERE user_id=:u"),
    ('pipeline_bootstrap', "UPDATE memory_pipeline_states SET bootstrap_status='collecting' WHERE user_id=:u"),
    ('pipeline_epoch', 'UPDATE memory_pipeline_states SET privacy_epoch=privacy_epoch+1 WHERE user_id=:u'),
    ('pipeline_repair', 'UPDATE memory_pipeline_states SET repair_generation=repair_generation+1 WHERE user_id=:u'),
    ('pipeline_regression', 'UPDATE memory_pipeline_states SET consolidated_through_turn_seq=0 WHERE user_id=:u'),
    ('registry_bookkeeping', 'UPDATE mem0_memory_registry SET revision=revision+1 WHERE user_id=:u'),
    ('registry_semantic', "UPDATE mem0_memory_registry SET semantic_status='superseded' WHERE user_id=:u"),
    ('source_authority', "UPDATE mem0_memory_sources SET authority='confirmed_user' WHERE user_id=:u"),
    ('source_delete', 'DELETE FROM mem0_memory_sources WHERE user_id=:u'),
    ('checkpoint', "UPDATE conversation_checkpoints SET validation_json=validation_json||jsonb_build_object('synthetic_note',true) WHERE id=:c"),
    ('prepared_contract', "UPDATE retention_prepared_contracts SET rendered_text=rendered_text||' synthetic' WHERE preparation_id=:p"),
    ('planned_candidate', "UPDATE mem0_ingest_candidates SET status='planned' WHERE user_id=:u"),
]


@pytest.mark.parametrize('label,sql', _MUTATIONS, ids=[v[0] for v in _MUTATIONS])
async def test_audited_semantic_or_processing_source_bumps_clock(online_promotion_db, label, sql):
    f = online_promotion_db
    async def case():
        async with f.maker() as writer:
            before = await revision(writer, f)
            await require(type(before) is int, 'registered_clock_missing')
            await mutate(writer, f, sql)
            after = await revision(writer, f)
            await require(after > before, 'semantic_source_clock_did_not_advance')
            await writer.commit()  # Only the fixture's synthetic source/revision.
    await checked(case)


_APPEND = """INSERT INTO messages(user_id,sender,kind,content,activity_date,turn_seq,turn_position)
 VALUES(:u,'user','normal','Synthetic later turn',CURRENT_DATE,2,1),
       (:u,'moly','normal','Synthetic later answer',CURRENT_DATE,2,2)"""
_IGNORED = [
    ('later_raw_append', _APPEND),
    ('last_committed', 'UPDATE chat_contexts SET last_committed_turn_seq=last_committed_turn_seq+1 WHERE user_id=:u'),
    ('forward_pipeline', 'UPDATE memory_pipeline_states SET source_through_turn_seq=2,ingest_through_turn_seq=2,consolidated_through_turn_seq=2 WHERE user_id=:u'),
    ('nickname', "UPDATE profiles SET nickname='Test' WHERE id=:u"),
    ('barrier_bookkeeping', 'UPDATE privacy_subject_barriers SET operation_id=gen_random_uuid() WHERE user_id=:u'),
]


@pytest.mark.parametrize('label,sql', _IGNORED, ids=[v[0] for v in _IGNORED])
async def test_ignored_forward_or_nonsemantic_change_preserves_clock(online_promotion_db, label, sql):
    f = online_promotion_db
    async def case():
        async with f.maker() as writer:
            before = await revision(writer, f)
            await mutate(writer, f, sql)
            await require(await revision(writer, f) == before, 'ignored_change_advanced_source_clock')
            await writer.commit()
    await checked(case)


async def test_legacy_checkpoint_cannot_dirty_other_preparation(online_promotion_db):
    f = online_promotion_db
    async def case():
        async with f.maker() as writer:
            before = await revision(writer, f)
            await writer.execute(text("UPDATE conversation_checkpoints SET summary=summary||' legacy' WHERE id=:c"), {'c': f.old})
            await require(await revision(writer, f) == before, 'legacy_projection_dirtied_private_preparation')
            await writer.commit()
    await checked(case)


_LOCK = 'SELECT revision FROM retention_preparation_revisions WHERE preparation_id=:p AND user_id=:u FOR UPDATE NOWAIT'


@pytest.mark.parametrize('label,sql,changed', [
    ('prefix', _MUTATIONS[0][1], True), ('append', _APPEND, False),
])
async def test_final_clock_first_orders_source_writer_after_release(online_promotion_db, label, sql, changed):
    f = online_promotion_db
    async def case():
        async with f.maker() as final, f.maker() as writer:
            before = await revision(final, f)
            await final.execute(text('SELECT id FROM retention_preparations WHERE id=:p FOR UPDATE NOWAIT'), {'p':f.identity})
            await final.execute(text(_LOCK), {'p': f.identity, 'u': f.uid})
            async def write_and_commit():
                await mutate(writer, f, sql)
                await writer.commit()
            task = asyncio.create_task(write_and_commit())
            try:
                await asyncio.sleep(.08)
                await require(not task.done(), 'source_writer_committed_before_final_clock_release')
                await require(await revision(final, f) == before, 'uncommitted_source_became_visible')
                # No global publication: releasing this lock is the modeled
                # commit/rollback point; the writer must linearize afterward.
                await final.rollback()
                await asyncio.wait_for(task, 3)
                async with f.maker() as verify:
                    after = await revision(verify, f)
                    await require((after > before) if changed else (after == before), 'writer_revision_result_invalid')
            finally:
                await final.rollback()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await writer.rollback()
    await checked(case)


@pytest.mark.parametrize('label,sql', [('prefix', _MUTATIONS[0][1]), ('append', _APPEND)])
async def test_writer_clock_first_makes_final_nowait_reject(online_promotion_db, label, sql):
    f = online_promotion_db
    async def case():
        async with f.maker() as writer, f.maker() as final:
            await mutate(writer, f, sql)
            rejected = False
            try:
                await final.execute(text('SELECT id FROM retention_preparations WHERE id=:p FOR UPDATE NOWAIT'), {'p':f.identity})
                await final.execute(text(_LOCK), {'p': f.identity, 'u': f.uid})
            except Exception as exc:
                rejected = getattr(getattr(exc, 'orig', None), 'sqlstate', None) == '55P03'
            finally:
                await final.rollback()
                await writer.rollback()
            await require(rejected, 'final_did_not_reject_uncommitted_source_clock_owner')
    await checked(case)


async def test_unregistered_writer_participates_in_registry_fence(online_promotion_db):
    f = online_promotion_db
    async def case():
        async with f.maker() as setup:
            await setup.execute(text('DELETE FROM retention_preparation_revisions WHERE preparation_id=:p AND user_id=:u'),
                                {'p': f.identity, 'u': f.uid})
            await setup.commit()  # Own synthetic clock only, to exercise missing-registration path.
        async with f.maker() as final, f.maker() as writer:
            await final.execute(text('SELECT id FROM retention_preparations WHERE id=:p FOR UPDATE NOWAIT'), {'p': f.identity})
            async def write_and_commit():
                await mutate(writer, f, _MUTATIONS[4][1])
                await writer.commit()
            task = asyncio.create_task(write_and_commit())
            try:
                await asyncio.sleep(.08)
                await require(not task.done(), 'unregistered_writer_bypassed_registry_fence')
                await final.rollback()
                await asyncio.wait_for(task, 3)
            finally:
                await final.rollback()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await writer.rollback()
    await checked(case)


async def aborted_plan(f):
    from app.services import retention_release_promotion as promotion
    async with f.maker() as s:
        await promotion.abort(s, f.identity)
        await s.commit()  # Only this owned private preparation.
    async with f.maker() as s:
        row = (await s.execute(promotion._USERS)).mappings().one()
        return await promotion.read_user(s, dict(row))


async def test_registration_rejects_repeatable_read_writer_with_old_sentinel_snapshot(online_promotion_db):
    import json
    import os
    from pathlib import Path
    import uuid
    from app.services import retention_release_promotion as promotion
    f = online_promotion_db
    async def case():
        plan = await aborted_plan(f)
        created, journal = None, None
        try:
            async with f.maker() as writer:
                await writer.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
                await writer.scalar(text("SELECT value FROM app_config WHERE key='context_retention_revision_fence'"))
                async with f.maker() as registration:
                    created, _ = await promotion.new_preparation(registration, plans=[plan], excluded=[],
                        project_ref='wywzjslvxwttxkecbyis', code_sha='a'*40)
                    directory = Path('/private/tmp/moly-online-promotion-dev-20261104')
                    journal = directory / f'registration-{uuid.uuid4().hex}.json'
                    fd = os.open(journal, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(fd, 'w') as stream:
                        json.dump({'schema':'owned-registration-dev-v1','user_id':str(f.uid),
                                   'preparation_id':str(created)}, stream)
                    await registration.commit()  # Private registry plus monotonic sentinel, no activation/config flag.
                rejected = False
                try:
                    await mutate(writer, f, _MUTATIONS[0][1])
                except Exception as exc:
                    rejected = getattr(getattr(exc, 'orig', None), 'sqlstate', None) == '40001'
                finally:
                    await writer.rollback()
                await require(rejected, 'old_repeatable_read_writer_bypassed_registration_fence')
        finally:
            if created is not None:
                async with f.maker() as cleanup:
                    await cleanup.execute(text('DELETE FROM retention_preparation_revision_events WHERE preparation_id=:p'), {'p':created})
                    await cleanup.execute(text('DELETE FROM retention_preparation_revisions WHERE preparation_id=:p'), {'p':created})
                    await cleanup.execute(text('DELETE FROM retention_preparations WHERE id=:p'), {'p':created})
                    await cleanup.commit()
                if journal is not None:
                    journal.unlink(missing_ok=True)
    await checked(case)


async def test_registration_nowait_rejects_writer_already_past_old_trigger(online_promotion_db):
    from app.services import retention_release_promotion as promotion
    f = online_promotion_db
    async def case():
        plan = await aborted_plan(f)
        async with f.maker() as writer, f.maker() as registration:
            # With no building preparation, the writer still holds the sentinel
            # shared lock even after taking the no-preparation fast path.
            await mutate(writer, f, _MUTATIONS[0][1])
            rejected = False
            try:
                await promotion.new_preparation(registration, plans=[plan], excluded=[],
                    project_ref='wywzjslvxwttxkecbyis', code_sha='a'*40)
            except Exception as exc:
                rejected = getattr(getattr(exc, 'orig', None), 'sqlstate', None) == '55P03'
            finally:
                await registration.rollback()
                await writer.rollback()
            await require(rejected, 'registration_bypassed_preexisting_source_writer')
    await checked(case)


@pytest.mark.parametrize('second_semantic', [True, False], ids=['semantic_event_fallback','ignored_registry_fallback'])
async def test_ordinary_writers_do_not_wait_on_each_others_clock_after_source_locks(online_promotion_db, second_semantic):
    import uuid
    f = online_promotion_db
    async def case():
        async with f.maker() as setup:
            first = await setup.scalar(text('SELECT id FROM mem0_memory_registry WHERE user_id=:u LIMIT 1'), {'u':f.uid})
            second = uuid.uuid4()
            await setup.execute(text('INSERT INTO mem0_memory_registry(id,user_id,provider,collection_version,'
                'provider_memory_id,source_turn_seq,content_hash,canonical_text,semantic_status,provider_delete_state,schema_version) '
                'SELECT :new,user_id,provider,collection_version,:provider_id,source_turn_seq,content_hash,canonical_text,'
                'semantic_status,provider_delete_state,schema_version FROM mem0_memory_registry WHERE id=:old'),
                {'new':second,'provider_id':uuid.uuid4(),'old':first})
            await setup.commit()  # Inert source row owned by this synthetic fixture.
        async with f.maker() as first_writer, f.maker() as second_writer:
            before = await revision(first_writer, f)
            await first_writer.execute(text('UPDATE mem0_memory_registry SET revision=revision+1 WHERE id=:r'), {'r':first})
            async def second_write():
                if second_semantic:
                    await second_writer.execute(text('UPDATE mem0_memory_registry SET revision=revision+1 WHERE id=:r'), {'r':second})
                else:
                    await second_writer.execute(text("UPDATE profiles SET nickname='Test2' WHERE id=:u"), {'u':f.uid})
                await second_writer.commit()
            task = asyncio.create_task(second_write())
            try:
                # With the old AFTER UPDATE mutex, this waits on first_writer's
                # clock while holding R2/profile and creates a new source cycle.
                await asyncio.wait_for(task, 2)
                if second_semantic:
                    await first_writer.execute(text('UPDATE mem0_memory_registry SET revision=revision+1 WHERE id=:r'), {'r':second})
                else:
                    await first_writer.execute(text("UPDATE profiles SET nickname='Test3' WHERE id=:u"), {'u':f.uid})
                await first_writer.commit()
                async with f.maker() as verify:
                    after = await revision(verify, f)
                    await require(after == before+(3 if second_semantic else 1), 'fallback_effective_revision_count_invalid')
            finally:
                await first_writer.rollback()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await second_writer.rollback()
    await checked(case)
