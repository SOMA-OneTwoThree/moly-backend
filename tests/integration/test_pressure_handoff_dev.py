"""Development Supabase only; synthetic state is rolled back, no provider calls."""
import json

import pytest
from sqlalchemy import text

from app.services import checkpoint_repo as repo
from tests.integration.test_checkpoint_continuity_dev import checkpoint_db as checkpoint_db
from tests.integration.test_context_pressure_dev import seed, seed_wake_dependency, wake_job_snapshot
from worker import checkpoint_jobs, memory_sweep_jobs


async def test_pressure_pending_wakes_latest_goal_without_rebinding_snapshot(checkpoint_db):
    session, (uid, _) = checkpoint_db
    ids, ingest = await seed_wake_dependency(session, uid)
    first = await repo.request_pressure(session, user_id=uid, generation=0, through_id=ids[1])
    assert first.status == 'queued'
    original = (await session.execute(text('SELECT payload FROM async_jobs WHERE id=:j'),
                                     {'j': first.job_id})).scalar_one()
    # The prior snapshot is already fixed. A new goal still wakes the exact dependency.
    await session.execute(text("UPDATE async_jobs SET available_at=now()+interval '180 seconds' WHERE id=:j"),
                          {'j': ingest})
    assert (await repo.request_pressure(session, user_id=uid, generation=0)).status == 'pending'
    now = (await session.execute(text('SELECT now()'))).scalar_one()
    assert (await wake_job_snapshot(session, ingest))['available_at'] == now
    assert (await session.execute(text('SELECT payload FROM async_jobs WHERE id=:j'),
                                  {'j': first.job_id})).scalar_one() == original


async def test_missing_ingest_link_created_once_using_current_repair_generation(checkpoint_db):
    session, (uid, _) = checkpoint_db
    ids, ingest = await seed_wake_dependency(session, uid)
    await session.execute(text('DELETE FROM async_jobs WHERE id=:j'), {'j': ingest})
    assert await repo.ensure_pressure_ingest(session, user_id=uid, generation=0, through_id=ids[-1])
    jobs = (await session.execute(text("SELECT * FROM async_jobs WHERE user_id=:u AND job_type='mem0_ingest'"),
                                  {'u': uid})).mappings().all()
    assert len(jobs) == 1 and jobs[0]['dedup_key'].endswith(':g7')
    assert jobs[0]['payload'] == {'turn_seq': 1, 'privacy_epoch': 0}
    assert not await repo.ensure_pressure_ingest(session, user_id=uid, generation=0, through_id=ids[-1])


@pytest.mark.parametrize('state', ['ready', 'running', 'dead', 'succeeded'])
async def test_existing_resume_family_never_gets_a_new_failure_budget(checkpoint_db, state):
    session, (uid, _) = checkpoint_db
    ids, ingest = await seed_wake_dependency(session, uid)
    await session.execute(text("""UPDATE async_jobs SET dedup_key=dedup_key||chr(58)||'r1', state=:state,
      attempt=1, available_at=now()+interval '1 hour',
      lease_owner=CASE WHEN :state='running' THEN 'synthetic' END,
      lease_token=CASE WHEN :state='running' THEN gen_random_uuid() END,
      lease_until=CASE WHEN :state='running' THEN now()+interval '1 minute' END
      WHERE id=:j"""), {'j': ingest, 'state': state})
    before = await wake_job_snapshot(session, ingest)
    assert not await repo.ensure_pressure_ingest(session, user_id=uid, generation=0, through_id=ids[-1])
    assert await wake_job_snapshot(session, ingest) == before
    assert (await session.execute(text("SELECT count(*) FROM async_jobs WHERE user_id=:u AND job_type='mem0_ingest'"),
                                  {'u': uid})).scalar_one() == 1


async def test_terminal_sweep_resume_family_prevents_new_base_budget(checkpoint_db):
    session, (uid, _) = checkpoint_db
    ids, ingest = await seed_wake_dependency(session, uid)
    await session.execute(text("UPDATE async_jobs SET dedup_key=:key,state='dead',attempt=3 WHERE id=:j"),
                          {'j': ingest, 'key': f'resume:{uid}:c0:g7:synthetic'})
    before = await wake_job_snapshot(session, ingest)
    assert not await repo.ensure_pressure_ingest(session, user_id=uid, generation=0, through_id=ids[-1])
    assert await wake_job_snapshot(session, ingest) == before


async def test_sweep_rechecks_stale_missing_snapshot_after_pressure_reserved_ingest(checkpoint_db):
    session, (uid, _) = checkpoint_db
    ids, ingest = await seed_wake_dependency(session, uid)
    await session.execute(text('DELETE FROM async_jobs WHERE id=:j'), {'j': ingest})
    stale_sweep = [(uid, 0, 7, 0, 1)]
    assert await repo.ensure_pressure_ingest(session, user_id=uid, generation=0, through_id=ids[-1])
    assert await memory_sweep_jobs._current_stalled(session, stale_sweep) == []


async def test_sweep_rejects_changed_cursor_or_repair_generation(checkpoint_db):
    session, (uid, _) = checkpoint_db
    await seed_wake_dependency(session, uid)
    assert await memory_sweep_jobs._current_stalled(session, [(uid, 1, 7, 0, 2)]) == []
    assert await memory_sweep_jobs._current_stalled(session, [(uid, 0, 8, 0, 1)]) == []


@pytest.mark.parametrize('error', [None, 'provider_failed'])
@pytest.mark.parametrize('initial', [False, True])
async def test_verified_dependency_only_wakes_pure_waiting_publisher(checkpoint_db, error, initial):
    session, (uid, _) = checkpoint_db
    await seed(session, uid)
    reserved = await repo.request_pressure(session, user_id=uid, generation=0)
    payload = (await session.execute(text('SELECT payload FROM async_jobs WHERE id=:j'),
                                    {'j': reserved.job_id})).scalar_one()
    payload['mode'] = 'publish_retention'
    await session.execute(text("""UPDATE async_jobs SET payload=CAST(:p AS jsonb), attempt=:attempt,
      state='ready', max_attempts=4, available_at=now()+interval '5 seconds',
      result_code=:result, last_error_code=:error WHERE id=:j"""),
      {'p': json.dumps(payload), 'error': error, 'j': reserved.job_id,
       'attempt': 0 if initial else 1,
       'result': None if initial else 'checkpoint_source_processing_pending'})
    before = await wake_job_snapshot(session, reserved.job_id)
    changed = await checkpoint_jobs.wake_ready_publisher(session, job_id=reserved.job_id,
        user_id=uid, payload=payload, event_epoch=0, event_generation=0)
    assert changed == (error is None)
    after = await wake_job_snapshot(session, reserved.job_id)
    assert {k:v for k,v in before.items() if k != 'available_at'} == {
        k:v for k,v in after.items() if k != 'available_at'}
    if error is None:
        assert after['available_at'] == (await session.execute(text('SELECT now()'))).scalar_one()
    else:
        assert after == before
