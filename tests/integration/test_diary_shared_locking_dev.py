"""Bounded two-session job-finalize/upsert race on the explicitly selected dev DB.

Synthetic fixture only. No model calls, shared config changes, or production targets.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.advisory_lock import advisory_xact_lock
from app.core.errors import AppError
from app.services import diary, diary_recall_repo, diary_shared_embeddings, jobs, privacy
from db.envfile import assert_dev_target, load_conn, project_ref

_REAL_ENSURE_ACTIVE = privacy.ensure_subject_active


@pytest.mark.parametrize("iteration", range(3))
async def test_finalizing_job_does_not_deadlock_same_dedup_upsert(monkeypatch, iteration):
    if os.environ.get("MOLY_DIARY_LOCKING_TEST_ENV") != "dev":
        pytest.skip("MOLY_DIARY_LOCKING_TEST_ENV=dev required")
    env_file = os.environ.get("MOLY_DIARY_LOCKING_ENV_FILE", ".env")
    dsn = load_conn(env_file)
    assert_dev_target(env_file, dsn)
    engine = create_async_engine(
        make_url(dsn).set(drivername="postgresql+asyncpg"),
        hide_parameters=True,
        connect_args={"statement_cache_size": 0, "command_timeout": 5,
                      "server_settings": {"statement_timeout": "3000",
                                          "lock_timeout": "2800",
                                          "application_name": "moly-diary-locking-dev"}},
    )
    uid, did, lease_token = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    created = False
    tasks = []
    result = {"iteration": iteration, "project": project_ref(dsn), "statement_timeout_ms": 3000,
              "model_calls": 0, "global_config_changes": 0, "runtime_functions": True}

    # Keep this test independent of another developer's selected mode. This is
    # process-local substitution, never an app_config mutation.
    async def private_mode(_session):
        return "private"

    monkeypatch.setattr(diary_shared_embeddings, "storage_mode", private_mode)
    real_enqueue = jobs.enqueue

    async def future_enqueue(*args, **kwargs):
        kwargs["available_at"] = datetime.now(timezone.utc) + timedelta(days=365)
        return await real_enqueue(*args, **kwargs)

    monkeypatch.setattr(jobs, "enqueue", future_enqueue)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO auth.users(id,created_at) VALUES(:u,now())"), {"u": uid})
            await conn.execute(text("""
                INSERT INTO diaries(id,user_id,diary_date,display_date,activity_date,
                                    source,kind,content,weather,published_at)
                VALUES(:d,:u,CURRENT_DATE,CURRENT_DATE,CURRENT_DATE,
                       'llm','shared_day','Synthetic locking fixture','sunny',now())
            """), {"d": did, "u": uid})
        created = True
        async with AsyncSession(engine) as session:
            await diary_recall_repo.upsert_diary_recall_document(session, user_id=uid, diary_id=did)
            await session.commit()
        async with engine.begin() as conn:
            row = (await conn.execute(text("""
                UPDATE async_jobs SET state='running',lease_owner='synthetic-locking-test',
                    lease_token=:token,lease_until=now()+interval '1 hour',attempt=1
                WHERE user_id=:u AND job_type='diary_recall_embed'
                RETURNING *
            """), {"token": lease_token, "u": uid})).mappings().one()
            claimed = jobs._job_from_row(row)
            document = (await conn.execute(text("""
                SELECT source_hash,suppression_generation,embedding_model,index_version
                FROM diary_recall_documents WHERE user_id=:u AND diary_id=:d
            """), {"u": uid, "d": did})).mappings().one()

        user_locked, finalize_fenced = asyncio.Event(), asyncio.Event()
        order = []

        async def concurrent_upsert():
            async with AsyncSession(engine) as session:
                async with session.begin():
                    await advisory_xact_lock(session, uid)
                    order.append("upsert_user_locked")
                    user_locked.set()
                    await asyncio.wait_for(finalize_fenced.wait(), timeout=5)
                    await diary_recall_repo.upsert_diary_recall_document(
                        session, user_id=uid, diary_id=did,
                    )
                    order.append("same_dedup_enqueue_completed")
                order.append("upsert_committed")
                return True

        async def concurrent_finalize():
            await asyncio.wait_for(user_locked.wait(), timeout=5)

            async def actual_writer(session):
                # finalize_success has already updated/locked its async_jobs row.
                order.append("finalize_job_fenced")
                finalize_fenced.set()
                return await diary_recall_repo.write_diary_embedding(
                    session, user_id=uid, diary_id=did,
                    source_hash=document["source_hash"],
                    generation=document["suppression_generation"],
                    embedding_model=document["embedding_model"],
                    index_version=document["index_version"],
                    vector=[1.0] + [0.0] * 1535,
                )

            async with AsyncSession(engine) as session:
                ok = await jobs.finalize_success(session, claimed, apply_domain=actual_writer)
                order.append("finalize_committed")
                return ok

        started = time.monotonic()
        tasks = [asyncio.create_task(concurrent_upsert()), asyncio.create_task(concurrent_finalize())]
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
        result["order"] = order
        result["outcomes"] = [type(x).__name__ if isinstance(x, BaseException) else x for x in outcomes]
        result["error_sqlstates"] = [getattr(getattr(x, "orig", x), "sqlstate", None)
                                     for x in outcomes if isinstance(x, BaseException)]
        async with engine.connect() as conn:
            final = (await conn.execute(text("""
                SELECT (SELECT count(*) FROM async_jobs WHERE user_id=:u) jobs,
                       (SELECT state FROM async_jobs WHERE id=:j) job_state,
                       (SELECT embedding IS NOT NULL FROM diary_recall_documents
                         WHERE user_id=:u AND diary_id=:d) private_vector_present
            """), {"u": uid, "j": claimed.id, "d": did})).mappings().one()
            result["final"] = dict(final)
        # Never let pytest render a DB driver exception's detail/bind values.
        assert result["outcomes"] == [True, True]
        assert final["jobs"] == 1 and final["job_state"] == "succeeded"
        assert final["private_vector_present"] is True
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if created:
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM privacy_ledger_events WHERE user_id=:u"), {"u": uid})
                await conn.execute(text("DELETE FROM auth.users WHERE id=:u"), {"u": uid})
                await conn.execute(text("DELETE FROM privacy_subject_barriers WHERE user_id=:u"), {"u": uid})
            async with engine.connect() as conn:
                remaining = (await conn.execute(text("""
                    SELECT (SELECT count(*) FROM auth.users WHERE id=:u)
                         + (SELECT count(*) FROM diaries WHERE user_id=:u)
                         + (SELECT count(*) FROM async_jobs WHERE user_id=:u)
                         + (SELECT count(*) FROM diary_recall_documents WHERE user_id=:u)
                """), {"u": uid})).scalar_one()
                result["fixture_rows_remaining"] = remaining
                assert remaining == 0
        await engine.dispose()
        evidence = os.environ.get("MOLY_DIARY_LOCKING_EVIDENCE")
        if evidence:
            path = Path(evidence)
            previous = json.loads(path.read_text()) if path.exists() else []
            previous = [x for x in previous if x["iteration"] != iteration]
            path.write_text(json.dumps(sorted(previous + [result], key=lambda x: x["iteration"]), indent=2) + "\n")
            path.chmod(0o600)


@pytest.mark.parametrize("delete_while_waiting", [False, True], ids=["profile-lock", "deletion-gate"])
async def test_welcome_locks_user_before_profile_fk_and_rechecks_deletion(monkeypatch, delete_while_waiting):
    if os.environ.get("MOLY_DIARY_LOCKING_TEST_ENV") != "dev":
        pytest.skip("MOLY_DIARY_LOCKING_TEST_ENV=dev required")
    env_file = os.environ.get("MOLY_DIARY_LOCKING_ENV_FILE", ".env")
    dsn = load_conn(env_file)
    assert_dev_target(env_file, dsn)
    engine = create_async_engine(
        make_url(dsn).set(drivername="postgresql+asyncpg"),
        hide_parameters=True,
        connect_args={"statement_cache_size": 0, "command_timeout": 5,
                      "server_settings": {"statement_timeout": "3000", "lock_timeout": "2800",
                                          "application_name": "moly-welcome-locking-dev"}},
    )
    uid = uuid.uuid4()
    created, tasks = False, []
    order = []
    welcome_attempt = asyncio.Event()
    owner_locked = asyncio.Event()
    case = "deletion-gate" if delete_while_waiting else "profile-lock"
    result = {"case": case, "project": project_ref(dsn), "model_calls": 0,
              "global_config_changes": 0, "statement_timeout_ms": 3000}

    def observe_insert(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.replace('"', '').lstrip().upper().startswith("INSERT INTO DIARIES"):
            order.append("welcome_insert")

    event.listen(engine.sync_engine, "before_cursor_execute", observe_insert)

    async def observed_user_lock(session, user_id):
        order.append("welcome_user_lock_attempt")
        welcome_attempt.set()
        await advisory_xact_lock(session, user_id)
        order.append("welcome_user_lock_acquired")

    async def private_mode(_session):
        return "private"

    real_enqueue = jobs.enqueue

    async def future_enqueue(*args, **kwargs):
        kwargs["available_at"] = datetime.now(timezone.utc) + timedelta(days=365)
        return await real_enqueue(*args, **kwargs)

    monkeypatch.setattr(diary, "advisory_xact_lock", observed_user_lock)
    monkeypatch.setattr(privacy, "ensure_subject_active", _REAL_ENSURE_ACTIVE)
    monkeypatch.setattr(diary_shared_embeddings, "storage_mode", private_mode)
    monkeypatch.setattr(jobs, "enqueue", future_enqueue)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO auth.users(id,created_at) VALUES(:u,now())"), {"u": uid})
            row = (await conn.execute(text("SELECT id,created_at,timezone,language FROM profiles WHERE id=:u"),
                                      {"u": uid})).mappings().one()
            profile = SimpleNamespace(**row)
        created = True

        async def profile_or_deletion_transaction():
            async with AsyncSession(engine) as session:
                async with session.begin():
                    await advisory_xact_lock(session, uid)
                    order.append("owner_user_locked")
                    owner_locked.set()
                    await asyncio.wait_for(welcome_attempt.wait(), timeout=5)
                    assert "welcome_insert" not in order
                    if delete_while_waiting:
                        await privacy.begin_subject_deletion(session, user_id=uid, operation_id=uuid.uuid4())
                        order.append("deletion_barrier_set")
                    else:
                        await session.execute(text("SELECT id FROM profiles WHERE id=:u FOR UPDATE"), {"u": uid})
                        order.append("profile_for_update_acquired")
                order.append("owner_committed")
                return True

        async def welcome_transaction():
            await asyncio.wait_for(owner_locked.wait(), timeout=5)
            async with AsyncSession(engine) as session:
                try:
                    inserted = await diary.ensure_welcome(session, profile)
                    await session.commit()
                    order.append("welcome_committed")
                    return inserted is not None
                except AppError as exc:
                    await session.rollback()
                    order.append("welcome_rejected")
                    return exc.code

        tasks = [asyncio.create_task(profile_or_deletion_transaction()),
                 asyncio.create_task(welcome_transaction())]
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)
        result["order"] = order
        result["outcomes"] = [type(x).__name__ if isinstance(x, BaseException) else x for x in outcomes]
        expected = [True, "ACCOUNT_DELETING"] if delete_while_waiting else [True, True]
        assert result["outcomes"] == expected
        async with engine.connect() as conn:
            counts = (await conn.execute(text("""
                SELECT (SELECT count(*) FROM diaries WHERE user_id=:u) diaries,
                       (SELECT count(*) FROM diary_recall_documents WHERE user_id=:u) documents,
                       (SELECT count(*) FROM async_jobs WHERE user_id=:u) jobs
            """), {"u": uid})).mappings().one()
            result["created_rows"] = dict(counts)
        if delete_while_waiting:
            assert dict(counts) == {"diaries": 0, "documents": 0, "jobs": 0}
            assert "welcome_insert" not in order
        else:
            assert dict(counts) == {"diaries": 1, "documents": 1, "jobs": 1}
            assert order.index("profile_for_update_acquired") < order.index("welcome_insert")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if created:
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM privacy_ledger_events WHERE user_id=:u"), {"u": uid})
                await conn.execute(text("DELETE FROM auth.users WHERE id=:u"), {"u": uid})
                await conn.execute(text("DELETE FROM privacy_subject_barriers WHERE user_id=:u"), {"u": uid})
            async with engine.connect() as conn:
                remaining = (await conn.execute(text("""
                    SELECT (SELECT count(*) FROM auth.users WHERE id=:u)
                         + (SELECT count(*) FROM diaries WHERE user_id=:u)
                         + (SELECT count(*) FROM async_jobs WHERE user_id=:u)
                         + (SELECT count(*) FROM diary_recall_documents WHERE user_id=:u)
                         + (SELECT count(*) FROM privacy_subject_barriers WHERE user_id=:u)
                """), {"u": uid})).scalar_one()
                result["fixture_rows_remaining"] = remaining
                assert remaining == 0
        await engine.dispose()
        evidence = os.environ.get("MOLY_DIARY_WELCOME_LOCKING_EVIDENCE")
        if evidence:
            path = Path(evidence)
            previous = json.loads(path.read_text()) if path.exists() else []
            previous = [x for x in previous if x["case"] != case]
            path.write_text(json.dumps(previous + [result], indent=2) + "\n")
            path.chmod(0o600)


async def test_sweep_document_recovery_skips_busy_users_without_waiting():
    from worker import memory_sweep_jobs as sweep

    if os.environ.get("MOLY_DIARY_LOCKING_TEST_ENV") != "dev":
        pytest.skip("MOLY_DIARY_LOCKING_TEST_ENV=dev required")
    env_file = os.environ.get("MOLY_DIARY_LOCKING_ENV_FILE", ".env")
    dsn = load_conn(env_file)
    assert_dev_target(env_file, dsn)
    engine = create_async_engine(
        make_url(dsn).set(drivername="postgresql+asyncpg"), hide_parameters=True,
        connect_args={"statement_cache_size": 0, "command_timeout": 5,
                      "server_settings": {"statement_timeout": "3000", "lock_timeout": "2800"}},
    )
    busy, free = uuid.uuid4(), uuid.uuid4()
    created = False
    result = {"project": project_ref(dsn), "model_calls": 0, "global_config_changes": 0}
    try:
        async with engine.begin() as conn:
            for uid in (busy, free):
                await conn.execute(text("INSERT INTO auth.users(id,created_at) VALUES(:u,now())"), {"u": uid})
        created = True
        async with AsyncSession(engine) as holder, AsyncSession(engine) as recovery:
            await advisory_xact_lock(holder, busy)
            started = time.monotonic()
            locked = await asyncio.wait_for(sweep._try_lock_document_users(
                recovery, [(free, uuid.uuid4()), (busy, uuid.uuid4()), (free, uuid.uuid4())],
            ), timeout=2)
            result["try_lock_elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
            result["busy_skipped"] = busy not in locked
            result["available_acquired"] = free in locked
            assert locked == {free}
            # The holder has not released its lock, while the sweep owns the
            # other key until its fenced transaction commits/rolls back.
            async with engine.connect() as observer:
                for uid in (busy, free):
                    acquired = (await observer.execute(sweep._TRY_DOCUMENT_USER_LOCK,
                                                       {"user_id": str(uid)})).scalar_one()
                    assert acquired is False
            await holder.rollback()
            retry = await sweep._try_lock_document_users(recovery, [(busy, uuid.uuid4())])
            assert retry == {busy}
            result["released_user_acquired_on_retry"] = True
            await recovery.rollback()
    finally:
        if created:
            async with engine.begin() as conn:
                for uid in (busy, free):
                    await conn.execute(text("DELETE FROM privacy_ledger_events WHERE user_id=:u"), {"u": uid})
                    await conn.execute(text("DELETE FROM auth.users WHERE id=:u"), {"u": uid})
                    await conn.execute(text("DELETE FROM privacy_subject_barriers WHERE user_id=:u"), {"u": uid})
            async with engine.connect() as conn:
                remaining = (await conn.execute(text("SELECT count(*) FROM auth.users WHERE id IN (:a,:b)"),
                                                {"a": busy, "b": free})).scalar_one()
                result["fixture_rows_remaining"] = remaining
                assert remaining == 0
        await engine.dispose()
        evidence = os.environ.get("MOLY_DIARY_SWEEP_LOCKING_EVIDENCE")
        if evidence:
            path = Path(evidence)
            path.write_text(json.dumps(result, indent=2) + "\n")
            path.chmod(0o600)
