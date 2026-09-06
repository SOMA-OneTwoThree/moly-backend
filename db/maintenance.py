"""Local maintenance helpers; no production request path imports this module."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import uuid

import asyncpg
from sqlalchemy import text
from sqlalchemy.dialects.postgresql.asyncpg import dialect

from app.services import jobs


class MaintenanceBlocked(RuntimeError):
    """The operator must inspect this subject before attempting another repair."""


async def lock_active_subject(session, user_id: uuid.UUID) -> int:
    """Lock deletion barrier and profile in the caller's short transaction."""
    await session.execute(text("SET LOCAL lock_timeout = '2s'"))
    await session.execute(text("SET LOCAL statement_timeout = '30s'"))
    await session.execute(text("SET LOCAL idle_in_transaction_session_timeout = '60s'"))
    barrier = (await session.execute(text('''
        SELECT state, epoch FROM public.privacy_subject_barriers
        WHERE user_id=:uid FOR UPDATE NOWAIT
    '''), {'uid': user_id})).first()
    if barrier is None or barrier[0] != 'active':
        raise MaintenanceBlocked('subject_not_active')
    profile = (await session.execute(text('''
        SELECT id FROM public.profiles WHERE id=:uid FOR UPDATE NOWAIT
    '''), {'uid': user_id})).first()
    if profile is None:
        raise MaintenanceBlocked('subject_missing')
    return int(barrier[1])


async def lock_enrollment_subject(session, user_id: uuid.UUID) -> int:
    """Allow an absent/legacy pipeline, but never cross an epoch or running job."""
    epoch = await lock_active_subject(session, user_id)
    state = (await session.execute(text('''
        SELECT privacy_epoch FROM memory_pipeline_states
        WHERE user_id=:uid FOR UPDATE NOWAIT
    '''), {'uid': user_id})).first()
    if state is not None and state[0] != epoch:
        raise MaintenanceBlocked('privacy_epoch_mismatch')
    active = (await session.execute(text('''
        SELECT state FROM async_jobs WHERE user_id=:uid
          AND state IN ('ready','running') AND queue IN ('memory','maintenance')
        FOR UPDATE NOWAIT
    '''), {'uid': user_id})).all()
    if any(row[0] == 'running' for row in active):
        raise MaintenanceBlocked('memory_job_running')
    return epoch


@dataclass
class _Result:
    row: asyncpg.Record | None

    def first(self):
        return self.row


class _JobSession:
    """Use the real jobs.enqueue/replay_dead on an existing asyncpg transaction.

    Only their parameterized TextClause + first-row interface is needed. Neither
    this adapter nor those domain functions owns a commit or a second connection.
    """

    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    async def execute(self, statement, params):
        compiled = statement.compile(dialect=dialect())
        row = await self.conn.fetchrow(str(compiled), *(params[k] for k in compiled.positiontup))
        return _Result(row)


@asynccontextmanager
async def locked_subject(conn: asyncpg.Connection, user_id: uuid.UUID):
    """Short exclusive repair window; fail closed on deletion, contention or work.

    NOWAIT avoids holding locks while waiting behind a live request. Lock queued
    jobs too: the consumer's SKIP LOCKED claim cannot take them during the repair.
    A running job is never interrupted by these tools.
    """
    async with conn.transaction():
        await conn.execute("SET LOCAL lock_timeout = '2s'")
        await conn.execute("SET LOCAL statement_timeout = '30s'")
        await conn.execute("SET LOCAL idle_in_transaction_session_timeout = '60s'")
        barrier = await conn.fetchrow('''
            SELECT state, epoch FROM public.privacy_subject_barriers
            WHERE user_id=$1 FOR UPDATE NOWAIT
        ''', user_id)
        if barrier is None or barrier['state'] != 'active':
            raise MaintenanceBlocked('subject_not_active')
        profile = await conn.fetchval('SELECT id FROM public.profiles WHERE id=$1 FOR UPDATE NOWAIT', user_id)
        state = await conn.fetchrow('''
            SELECT * FROM public.memory_pipeline_states WHERE user_id=$1 FOR UPDATE NOWAIT
        ''', user_id)
        if profile is None or state is None:
            raise MaintenanceBlocked('subject_or_pipeline_missing')
        if state['privacy_epoch'] != barrier['epoch']:
            raise MaintenanceBlocked('privacy_epoch_mismatch')
        if state['mode'] == 'legacy' or state['bootstrap_status'] != 'ready':
            raise MaintenanceBlocked('pipeline_not_ready')
        active = await conn.fetch('''
            SELECT id, state FROM public.async_jobs
            WHERE user_id=$1 AND state IN ('ready','running')
              AND job_type IN ('mem0_ingest','mem0_consolidate','mem0_reconsolidate',
                               'mem0_provider_delete') FOR UPDATE NOWAIT
        ''', user_id)
        if any(row['state'] == 'running' for row in active):
            raise MaintenanceBlocked('memory_job_running')
        yield state


async def enqueue_recovery(conn: asyncpg.Connection, *, job_type: str, user_id: uuid.UUID,
                           dedup_key: str, payload: dict, priority: int = 100) -> uuid.UUID | None:
    """Canonical queue/payload/hash and replay lineage; never revive terminal rows."""
    if not conn.is_in_transaction():
        raise MaintenanceBlocked('repair_transaction_required')
    queues = {'mem0_ingest': jobs.QUEUE_MEMORY, 'mem0_consolidate': jobs.QUEUE_MEMORY,
              'mem0_provider_delete': jobs.QUEUE_MAINTENANCE}
    session = _JobSession(conn)
    result = await jobs.enqueue(session, queue=queues[job_type], job_type=job_type,
                                user_id=user_id, dedup_key=dedup_key, payload=payload,
                                priority=priority)
    if result is not None:
        return result
    previous = await conn.fetchrow('''
        SELECT id, state, payload, queue FROM public.async_jobs WHERE job_type=$1 AND dedup_key=$2
    ''', job_type, dedup_key)
    if previous['state'] in {'ready', 'running'}:
        return None
    old_payload = json.loads(previous['payload'])
    if (previous['state'] != 'dead' or previous['queue'] != queues[job_type]
            or any(old_payload.get(k) != v for k, v in payload.items())):
        raise MaintenanceBlocked('terminal_job_requires_review')
    # An existing replay (including a failed or completed one) must be reviewed;
    # retrying this command does not invent an unrelated dedup key to evade it.
    if await conn.fetchval('SELECT EXISTS(SELECT 1 FROM public.async_jobs WHERE replay_of=$1)', previous['id']):
        raise MaintenanceBlocked('existing_replay_requires_review')
    result = await jobs.replay_dead(session, job_id=previous['id'], operation_id=uuid.uuid4())
    if result is None:
        raise MaintenanceBlocked('expired_or_redacted_job')
    return result


async def cancel_ready_extraction(conn: asyncpg.Connection, user_id: uuid.UUID) -> None:
    """Called inside locked_subject, which has already rejected running work."""
    await conn.execute('''
        UPDATE public.async_jobs SET state='cancelled', finished_at=now(),
          result_code='manual_reextract', lease_owner=NULL, lease_token=NULL, lease_until=NULL
        WHERE user_id=$1 AND job_type IN ('mem0_ingest','mem0_consolidate') AND state='ready'
    ''', user_id)
