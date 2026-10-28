"""Opt-in shared-diary invariants on the existing isolated development Supabase.

Run only with MOLY_DIARY_SHARED_TEST_ENV=dev and an absolute
MOLY_DIARY_SHARED_ENV_FILE. No scratch or production fallback is supported.
All user/ment/shared-model rows are synthetic, tracked and removed in teardown;
all outbox work is future-dated and the embedding provider is forbidden.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import statistics
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.advisory_lock import advisory_xact_lock
from app.core.errors import AppError
from app.models.diary import Diary
from app.models.profile import Profile
from app.services import diary_recall_repo as repo
from app.services import diary_shared_embeddings as shared
from app.services import diary, diary_generation, jobs, privacy, recall_diaries
from app.services.llm import LLMResult
from db.envfile import assert_dev_target, load_conn
from worker import diary_recall_jobs, memory_sweep_jobs as sweep
from scripts import diary_shared_embeddings as backfill

_REAL_ENSURE_ACTIVE = privacy.ensure_subject_active
_DAY = date(2026, 10, 1)


def _vector(index: int) -> list[float]:
    result = [0.0] * shared.DIMENSIONS
    result[index] = 1.0
    return result


def _literal(vector: list[float]) -> str:
    return "[" + ",".join(format(v, ".9g") for v in vector) + "]"


@pytest_asyncio.fixture
async def shared_db(monkeypatch):
    if os.environ.get("MOLY_DIARY_SHARED_TEST_ENV") != "dev":
        pytest.skip("explicit MOLY_DIARY_SHARED_TEST_ENV=dev required")
    if os.environ.get("MOLY_SCHEMA_TEST_DSN"):
        pytest.fail("existing dev DB required; scratch DSN is forbidden")
    env = os.environ.get("MOLY_DIARY_SHARED_ENV_FILE", "")
    if not env or not Path(env).is_absolute():
        pytest.fail("absolute MOLY_DIARY_SHARED_ENV_FILE required")
    dsn = load_conn(env)
    assert_dev_target(env, dsn)
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=15, command_timeout=30)
    engine = create_async_engine(
        make_url(dsn).set(drivername="postgresql+asyncpg"),
        connect_args={"statement_cache_size": 0}, pool_size=3, max_overflow=0,
    )
    users, ments = [], []
    state = SimpleNamespace(mode="private")
    tag = uuid.uuid4().hex
    model = f"qa-diary-shared-{tag}"
    monkeypatch.setattr(repo.settings, "embedder_model", model)
    monkeypatch.setattr(privacy, "ensure_subject_active", _REAL_ENSURE_ACTIVE)
    provider = AsyncMock(side_effect=AssertionError("integration must not call the provider"))
    monkeypatch.setattr(repo, "embed_text", provider)

    async def mode(session):
        return state.mode

    monkeypatch.setattr(shared, "storage_mode", mode)
    original_enqueue = jobs.enqueue

    async def enqueue_later(session, **kwargs):
        kwargs["available_at"] = datetime.now(timezone.utc) + timedelta(days=365)
        return await original_enqueue(session, **kwargs)

    async def enqueue_many_later(session, *, items, **kwargs):
        count = 0
        for uid, key, payload in items:
            count += await enqueue_later(
                session, user_id=uid, dedup_key=key, payload=payload, **kwargs,
            ) is not None
        return count

    monkeypatch.setattr(jobs, "enqueue", enqueue_later)
    monkeypatch.setattr(jobs, "enqueue_many", enqueue_many_later)
    original_upsert = repo.upsert_diary_recall_document

    async def pinned_upsert(session, *, user_id, diary_id):
        await original_upsert(session, user_id=user_id, diary_id=diary_id)
        # An old deployed development sweep must not generate network work for
        # intentionally NULL fixture vectors before the new code is deployed.
        await session.execute(text(
            "UPDATE diary_recall_documents SET embedding_repair_attempts=3 "
            "WHERE user_id=:uid AND diary_id=:did AND embedding IS NULL"
        ), {"uid": user_id, "did": diary_id})

    monkeypatch.setattr(repo, "upsert_diary_recall_document", pinned_upsert)
    week = date(4000, 1, 3) + timedelta(weeks=uuid.uuid4().int % 10000)

    async def user(language="ko"):
        uid = uuid.uuid4()
        await conn.execute("INSERT INTO auth.users(id,created_at) VALUES($1,now())", uid)
        users.append(uid)
        await conn.execute(
            "UPDATE profiles SET language=$2,timezone='Asia/Seoul',nickname='QA' WHERE id=$1", uid, language,
        )
        return uid

    async def ment(content, *, sequence=1, week_override=None):
        mid = uuid.uuid4()
        await conn.execute(
            "INSERT INTO moly_life_ments(id,content,weather,week_start_date,sequence_no) "
            "VALUES($1,$2,'sunny',$3,$4)", mid, content,
            (week_override or week) + timedelta(weeks=(sequence - 1) // 2), (sequence - 1) % 2 + 1,
        )
        ments.append(mid)
        return mid

    async def diary(uid, content, *, kind="capi_day", preset=None, day=_DAY, vector=None, title=None):
        did = uuid.uuid4()
        async with AsyncSession(engine, expire_on_commit=False) as session:
            session.add(Diary(
                id=did, user_id=uid, diary_date=day, display_date=day,
                activity_date=None if kind == "welcome" else day,
                kind=kind, source={"capi_day": "preset", "welcome": "welcome", "shared_day": "llm"}[kind],
                preset_ment_id=preset, title=title, content=content, weather="sunny",
                published_at=datetime(2026, 10, 1, tzinfo=timezone.utc), record_status="published",
            ))
            await session.flush()
            await repo.upsert_diary_recall_document(session, user_id=uid, diary_id=did)
            await session.commit()
        if vector is not None:
            await conn.execute(
                "UPDATE diary_recall_documents SET embedding=$3::public.vector(1536) "
                "WHERE user_id=$1 AND diary_id=$2 AND embedding_model=$4",
                uid, did, _literal(vector), model,
            )
        return did

    async def ensure(uid, did):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            routed = await shared.ensure_for_diary(session, user_id=uid, diary_id=did)
            await session.commit()
            return routed

    try:
        assert await conn.fetchval("SELECT to_regclass('public.diary_template_embeddings') IS NOT NULL")
        yield SimpleNamespace(
            conn=conn, engine=engine, user=user, ment=ment, diary=diary,
            ensure=ensure, state=state, tag=tag, model=model, provider=provider,
        )
    finally:
        # Ownerless public-copy jobs must be removed explicitly: they are not
        # covered by the synthetic users' profile/async_jobs cascade.
        await conn.execute(
            "DELETE FROM async_jobs WHERE user_id IS NULL AND payload->>'shared_id' IN "
            "(SELECT id::text FROM diary_template_embeddings WHERE embedding_model=$1)", model,
        )
        for uid in users:
            await conn.execute("DELETE FROM privacy_ledger_events WHERE user_id=$1", uid)
            await conn.execute("DELETE FROM auth.users WHERE id=$1", uid)
            await conn.execute("DELETE FROM privacy_subject_barriers WHERE user_id=$1", uid)
        for mid in ments:
            await conn.execute("DELETE FROM moly_life_ments WHERE id=$1", mid)
        await conn.execute("DELETE FROM diary_template_embeddings WHERE embedding_model=$1", model)
        remaining = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM auth.users WHERE id=ANY($1::uuid[]) "
            "UNION ALL SELECT 1 FROM diaries WHERE user_id=ANY($1::uuid[]) "
            "UNION ALL SELECT 1 FROM diary_recall_documents WHERE user_id=ANY($1::uuid[]) "
            "UNION ALL SELECT 1 FROM async_jobs WHERE user_id=ANY($1::uuid[]) "
            "UNION ALL SELECT 1 FROM moly_life_ments WHERE id=ANY($2::uuid[]) "
            "UNION ALL SELECT 1 FROM diary_template_embeddings WHERE embedding_model=$3)",
            users, ments, model,
        )
        await engine.dispose()
        await conn.close()
        assert not remaining, "synthetic shared-diary data remains"


async def _projection(db, uid, did):
    return await db.conn.fetchrow(
        "SELECT source_hash,suppression_generation,embedding::text vector,shared_embedding_id,embedding_model "
        "FROM diary_recall_documents WHERE user_id=$1 AND diary_id=$2", uid, did,
    )


async def _write(db, uid, did, loaded, vector):
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        result = await repo.write_diary_embedding(
            session, user_id=uid, diary_id=did,
            source_hash=loaded["source_hash"], generation=loaded["suppression_generation"],
            embedding_model=loaded["embedding_model"], index_version=repo.INDEX_VERSION, vector=vector,
        )
        await session.commit()
        return result


async def _snapshot(db, cases):
    snapshots, elapsed = [], []
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        for uid, options in cases:
            start = time.perf_counter()
            response = await recall_diaries.recall(session, uid, **options)
            query = None if options.get("focus_id") else options.get("query")
            rows = (await session.execute(recall_diaries._RECALL, {
                "user_id": uid, "query": query.strip() if query else None,
                "embedding": recall_diaries._vector_literal(options.get("query_embedding")),
                "min_similarity": 0.25, "from_date": options.get("from_date"),
                "to_date": options.get("to_date"), "focus_id": options.get("focus_id"),
                "now": datetime.now(timezone.utc), "limit": max(1, min(options.get("limit", 3), 5)),
            })).mappings().all()
            elapsed.append(time.perf_counter() - start)
            # Function output plus actual SQL score/count ordering must stay the same.
            snapshots.append((json.dumps(response, ensure_ascii=False, sort_keys=True), [dict(row) for row in rows]))
        await session.rollback()
    return snapshots, elapsed


async def test_200_queries_preserve_three_language_scores_bodies_counts_and_ownership(shared_db):
    db = shared_db
    owners, expected_ids, variants, unshared = [], {}, [], []
    texts = [
        ("ko", "공원 산책 뒤 귤을 먹었어", "산책", "창가에서 퍼즐을 맞췄어"),
        ("ja", "公園を散歩してみかんを食べた", "散歩", "窓辺でパズルをした"),
        ("en", "I ate a mandarin after a park walk", "park", "I did a puzzle by the window"),
    ]
    for index, (language, main, term, other) in enumerate(texts):
        main, other = f"{main} [public-fixture:{db.tag}]", f"{other} [public-fixture:{db.tag}]"
        first = await db.ment(main, sequence=2 * index + 1)
        # Each ment uses a different future week to keep approved 1/2 weekly slots.
        second = await db.ment(other, sequence=2 * index + 2)
        for _ in range(2):
            uid = await db.user(language)
            ids = [
                await db.diary(uid, main, preset=first, day=_DAY, vector=_vector(0)),
                await db.diary(uid, main, preset=first, day=_DAY + timedelta(days=1), vector=_vector(0)),
                await db.diary(uid, main, preset=first, day=_DAY + timedelta(days=2), vector=_vector(1)),
                await db.diary(uid, other, preset=second, day=_DAY + timedelta(days=3), vector=_vector(1)),
                await db.diary(uid, f"private fixture {db.tag}", kind="shared_day", day=_DAY + timedelta(days=4), vector=_vector(0)),
                await db.diary(uid, main, preset=first, title="unapproved private title", day=_DAY + timedelta(days=5), vector=_vector(1)),
            ]
            title, body = shared.approved_welcome_pairs()[index]
            ids.append(await db.diary(uid, body, kind="welcome", title=title, day=_DAY - timedelta(days=1), vector=_vector(0)))
            owners.append((uid, term, ids))
            expected_ids[uid] = {str(i) for i in ids}
            variants.append((uid, ids[2]))
            unshared.extend([(uid, ids[4]), (uid, ids[5])])
    cases = []
    for i in range(200):
        uid, term, ids = owners[i % len(owners)]
        query, vector = [(None, None), (term, None), ("semantic fixture", _vector(0)),
                         ("semantic fixture", _vector(1)), ("no such content", _vector(2))][i % 5]
        options = {"query": query, "query_embedding": vector, "need": ["summary", "full", "quote", "full_card"][i % 4], "limit": i % 7}
        if i % 8 == 0:
            options.update(from_date=_DAY + timedelta(days=1), to_date=_DAY + timedelta(days=3))
        if i % 9 == 0:
            options["focus_id"] = ids[(i // 9) % len(ids)]
        cases.append((uid, options))
    before, private_latency = await _snapshot(db, cases)
    for (uid, _), (wire, _rows) in zip(cases, before, strict=True):
        assert {item["id"] for item in json.loads(wire)["items"]} <= expected_ids[uid]
    db.state.mode = "dual"
    for uid, _term, ids in owners:
        for did in ids:
            await db.ensure(uid, did)
    dual, dual_latency = await _snapshot(db, cases)
    assert dual == before
    for uid, did in variants + unshared:
        row = await _projection(db, uid, did)
        assert row["vector"] is not None and row["shared_embedding_id"] is None
    db.state.mode = "shared"
    # Only synthetic, validated equal vectors are pruned; no broad backfill tool
    # or real member rows are touched by this integration fixture.
    pruned = await db.conn.fetch(
        "UPDATE diary_recall_documents rd SET embedding=NULL,embedding_repair_attempts=3,updated_at=now() "
        "FROM diary_template_embeddings se WHERE rd.user_id=ANY($1::uuid[]) "
        "AND rd.embedding_model=$2 AND rd.shared_embedding_id=se.id "
        "AND se.source_hash=rd.source_hash AND se.embedding_model=rd.embedding_model "
        "AND se.index_version=rd.index_version AND se.dimensions=1536 AND se.search_text=rd.search_text "
        "AND rd.embedding=se.embedding AND rd.suppression_generation=0 "
        "AND NOT EXISTS(SELECT 1 FROM privacy_subject_barriers b WHERE b.user_id=rd.user_id AND b.state<>'active') "
        "RETURNING rd.diary_id", [o[0] for o in owners], db.model,
    )
    assert len(pruned) == 24
    after, shared_latency = await _snapshot(db, cases)
    assert after == before
    for uid, did in variants + unshared:
        assert (await _projection(db, uid, did))["vector"] is not None
    db.provider.assert_not_awaited()
    result_path = os.environ.get("MOLY_DIARY_SHARED_RESULT_PATH")
    if result_path:
        requested_destination = Path(result_path)
        destination = requested_destination.resolve()
        if not requested_destination.is_absolute() or Path.cwd().resolve() in destination.parents:
            pytest.fail("result evidence must use an absolute path outside the repository")

        def summary(values):
            return {
                "samples": len(values), "p50_ms": statistics.median(values) * 1000,
                "p95_ms": statistics.quantiles(values, n=20)[18] * 1000,
                "max_ms": max(values) * 1000,
            }

        latencies = {"private": summary(private_latency), "dual": summary(dual_latency), "shared": summary(shared_latency)}
        baseline_p95 = latencies["private"]["p95_ms"]
        allowed_increase = max(baseline_p95 * 0.10, 20.0)
        result = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "query_cases": 200, "languages": ["ko", "ja", "en"],
            "phases": ["private", "dual", "shared"], "phase_result_parity": True,
            "raw_score_count_order_parity": True, "pruned_fixture_vectors": len(pruned),
            "private_variants_preserved": len(variants), "unapproved_private_documents_preserved": len(unshared),
            "provider_calls": db.provider.await_count, "latencies": latencies,
            "performance_gate": {
                "comparison": "shared vs private p95", "allowed_increase_ms": allowed_increase,
                "observed_increase_ms": latencies["shared"]["p95_ms"] - baseline_p95,
                "passed": latencies["shared"]["p95_ms"] <= baseline_p95 + allowed_increase,
                "measurement": "end-to-end function plus raw SQL validation over existing dev connection",
            },
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        destination.chmod(0o600)


async def test_ready_canonical_never_replaces_a_different_private_vector(shared_db):
    db = shared_db
    uid, other = await db.user(), await db.user()
    content = f"approved shared copy {db.tag}"
    mid = await db.ment(content)
    first = await db.diary(uid, content, preset=mid, vector=_vector(0))
    variant = await db.diary(other, content, preset=mid, vector=_vector(1))
    db.state.mode = "dual"
    assert await db.ensure(uid, first)
    canonical = (await _projection(db, uid, first))["shared_embedding_id"]
    original = await db.conn.fetchval("SELECT embedding::text FROM diary_template_embeddings WHERE id=$1", canonical)
    assert await db.ensure(other, variant)
    assert await db.conn.fetchval("SELECT embedding::text FROM diary_template_embeddings WHERE id=$1", canonical) == original
    row = await _projection(db, other, variant)
    assert row["vector"] == _literal(_vector(1)) and row["shared_embedding_id"] is None


async def test_hash_change_clears_shared_pointer_and_late_old_write_is_rejected(shared_db):
    db = shared_db
    uid = await db.user()
    content = f"approved before-edit {db.tag}"
    mid = await db.ment(content)
    did = await db.diary(uid, content, preset=mid, vector=_vector(0))
    db.state.mode = "dual"
    await db.ensure(uid, did)
    loaded = await _projection(db, uid, did)
    assert loaded["shared_embedding_id"]
    await db.conn.execute("UPDATE diaries SET content=$3 WHERE user_id=$1 AND id=$2", uid, did, f"unapproved edited copy {db.tag}")
    db.state.mode = "shared"
    async with AsyncSession(db.engine) as session:
        await repo.upsert_diary_recall_document(session, user_id=uid, diary_id=did)
        await session.commit()
    changed = await _projection(db, uid, did)
    assert changed["source_hash"] != loaded["source_hash"]
    assert changed["vector"] is None and changed["shared_embedding_id"] is None
    assert not await _write(db, uid, did, loaded, _vector(1))
    assert (await _projection(db, uid, did))["vector"] is None


@pytest.mark.parametrize("race", ["generation", "deletion"])
async def test_late_private_write_rechecks_after_waiting_for_user_lock(shared_db, race):
    db = shared_db
    uid = await db.user()
    did = await db.diary(uid, f"private delayed write {db.tag}", kind="shared_day", vector=_vector(0))
    loaded = await _projection(db, uid, did)
    task = None
    try:
        async with AsyncSession(db.engine) as blocker:
            await advisory_xact_lock(blocker, uid)
            task = asyncio.create_task(_write(db, uid, did, loaded, _vector(1)))
            await asyncio.sleep(0.05)
            assert not task.done()
            if race == "generation":
                await blocker.execute(text("INSERT INTO chat_contexts(user_id,memory_generation) VALUES(:uid,1) "
                                           "ON CONFLICT(user_id) DO UPDATE SET memory_generation=chat_contexts.memory_generation+1"), {"uid": uid})
            else:
                await blocker.execute(text("INSERT INTO privacy_subject_barriers(user_id,state,operation_id,epoch) "
                                           "VALUES(:uid,'deleting',:operation,1) ON CONFLICT(user_id) DO UPDATE "
                                           "SET state='deleting',operation_id=EXCLUDED.operation_id,epoch=privacy_subject_barriers.epoch+1"),
                                      {"uid": uid, "operation": uuid.uuid4()})
            await blocker.commit()
        assert await asyncio.wait_for(task, 15) is False
        assert (await _projection(db, uid, did))["vector"] == loaded["vector"]
        if race == "deletion":
            async with AsyncSession(db.engine) as session:
                with pytest.raises(AppError) as exc:
                    await recall_diaries.recall(session, uid, query=None)
                assert exc.value.code == "ACCOUNT_DELETING"
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_shared_ready_and_pending_do_not_regenerate_private_vectors(shared_db):
    db = shared_db
    uid = await db.user()
    content = f"public ready repair fixture {db.tag}"
    mid = await db.ment(content)
    ready = await db.diary(uid, content, preset=mid, vector=_vector(0))
    db.state.mode = "dual"
    await db.ensure(uid, ready)
    await db.conn.execute("UPDATE diary_recall_documents SET embedding=NULL,embedding_repair_attempts=3 WHERE user_id=$1 AND diary_id=$2", uid, ready)
    pending_content = f"public pending repair fixture {db.tag}"
    pending_mid = await db.ment(pending_content, sequence=2)
    db.state.mode = "shared"
    pending = await db.diary(uid, pending_content, preset=pending_mid, day=_DAY + timedelta(days=1))
    pending_id = (await _projection(db, uid, pending))["shared_embedding_id"]
    assert pending_id
    private_sql = text(str(sweep._MISSING_EMBEDDING).replace(
        "ORDER BY rd.updated_at", "AND rd.user_id=:qa_user ORDER BY rd.updated_at", 1,
    ))
    async with AsyncSession(db.engine) as session:
        # Make the private retry condition eligible only inside this rolled-back
        # transaction, so the new shared exclusion is genuinely exercised.
        await session.execute(text("UPDATE diary_recall_documents SET embedding_repair_attempts=0 WHERE user_id=:uid"), {"uid": uid})
        missing = (await session.execute(private_sql, {
            "qa_user": uid, "limit": 50, "max_attempts": sweep.MAX_EMBEDDING_REPAIR,
        })).all()
        assert missing == []
        await session.rollback()
    # End only this fixture's ownerless original job, then exercise an aged retry.
    await db.conn.execute(
        "UPDATE async_jobs SET state='dead',finished_at=now() "
        "WHERE user_id IS NULL AND payload->>'shared_id'=$1", str(pending_id),
    )
    await db.conn.execute("UPDATE diary_template_embeddings SET updated_at=now()-interval '90 days' WHERE id=$1", pending_id)
    global_sql = text(str(sweep._MISSING_SHARED_EMBEDDING).replace(
        "ORDER BY se.updated_at", "AND se.embedding_model=:qa_model ORDER BY se.updated_at", 1,
    ))
    async with AsyncSession(db.engine) as session:
        missing = (await session.execute(global_sql, {
            "qa_model": db.model, "limit": 50, "max_attempts": sweep.MAX_EMBEDDING_REPAIR,
        })).all()
        assert missing == [(pending_id, 0)]
        assert await sweep._enqueue_shared_repairs(session, missing) == 1
        await session.commit()
    job = await db.conn.fetchrow(
        "SELECT user_id,available_at FROM async_jobs WHERE job_type='diary_template_embed' "
        "AND dedup_key=$1", f"diarytemplate:repair:{pending_id}:1",
    )
    assert job["user_id"] is None
    assert job["available_at"] > datetime.now(timezone.utc) + timedelta(days=364)
    assert await db.conn.fetchval("SELECT embedding_repair_attempts FROM diary_template_embeddings WHERE id=$1", pending_id) == 1
    # A concurrent/stale second sweep must not consume another retry.
    async with AsyncSession(db.engine) as session:
        assert await sweep._enqueue_shared_repairs(session, [(pending_id, 0)]) == 0
        await session.commit()
    db.provider.assert_not_awaited()


async def test_restore_preserves_visible_stale_generation_shared_only_history(shared_db):
    db = shared_db
    uid = await db.user()
    content = f"public stale generation restore fixture {db.tag}"
    mid = await db.ment(content)
    did = await db.diary(uid, content, preset=mid, vector=_vector(0))
    db.state.mode = "dual"
    await db.ensure(uid, did)
    projection = await _projection(db, uid, did)
    await db.conn.execute("UPDATE diary_recall_documents SET embedding=NULL,embedding_repair_attempts=3 WHERE user_id=$1 AND diary_id=$2", uid, did)
    await db.conn.execute("INSERT INTO chat_contexts(user_id,memory_generation) VALUES($1,1) "
                          "ON CONFLICT(user_id) DO UPDATE SET memory_generation=1", uid)
    pairs = shared.approved_welcome_pairs()
    welcome = ([p[0] for p in pairs], [p[1] for p in pairs])
    candidate = await db.conn.fetchrow(backfill.base_sql("restore") + " AND rd.user_id=$3 AND rd.diary_id=$4", *welcome, uid, did)
    assert candidate is not None and candidate["suppression_generation"] == 0
    vector = await db.conn.fetchval("SELECT embedding::text FROM diary_template_embeddings WHERE id=$1", projection["shared_embedding_id"])
    row = {
        "source_hash": candidate["source_hash"], "embedding_model": candidate["embedding_model"],
        "index_version": candidate["index_version"],
        "search_digest": hashlib.sha256(candidate["search_text"].encode()).hexdigest(),
        "vector_digest": hashlib.sha256(vector.encode()).hexdigest(),
    }
    async with db.conn.transaction():
        assert await backfill.mutate_one(db.conn, "restore", row, candidate, welcome)
    restored = await _projection(db, uid, did)
    assert restored["vector"] == vector
    assert restored["suppression_generation"] == 0
    assert await db.conn.fetchval("SELECT memory_generation FROM chat_contexts WHERE user_id=$1", uid) == 1


async def _profile(db, uid):
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        return await session.get(Profile, uid)


async def _detail(db, uid, did):
    async with AsyncSession(db.engine) as session:
        return await diary.get_diary(session, str(uid), str(did))


async def _owned_embed_count(db, uid, did):
    return await db.conn.fetchval(
        "SELECT count(*) FROM async_jobs WHERE user_id=$1 AND job_type='diary_recall_embed' "
        "AND payload->>'diary_id'=$2", uid, str(did),
    )


async def _finish_fixture_embeddings(db, uid, did, monkeypatch):
    """Real handlers/repo/CAS, fake provider, no consumer or network dispatch."""
    embed = AsyncMock(return_value=_vector(0))
    monkeypatch.setattr(repo, "embed_text", embed)
    monkeypatch.setattr(
        diary_recall_jobs, "get_sessionmaker",
        lambda: lambda: AsyncSession(db.engine, expire_on_commit=False),
    )
    projection = await _projection(db, uid, did)
    sid = projection["shared_embedding_id"]
    if sid and not await db.conn.fetchval(
        "SELECT embedding IS NOT NULL FROM diary_template_embeddings WHERE id=$1", sid,
    ):
        job = await db.conn.fetchrow(
            "SELECT id,user_id FROM async_jobs WHERE job_type='diary_template_embed' "
            "AND user_id IS NULL AND payload->>'shared_id'=$1 LIMIT 1", str(sid),
        )
        assert job is not None
        result = await diary_recall_jobs.handle_diary_template_embed(SimpleNamespace(
            id=job["id"], user_id=job["user_id"], payload={"shared_id": str(sid)},
        ))
        assert result.apply_domain is not None
        async with AsyncSession(db.engine) as session:
            await result.apply_domain(session)
            await session.commit()
    job = await db.conn.fetchrow(
        "SELECT id FROM async_jobs WHERE job_type='diary_recall_embed' AND user_id=$1 "
        "AND payload->>'diary_id'=$2 LIMIT 1", uid, str(did),
    )
    if job:
        result = await diary_recall_jobs.handle_diary_recall_embed(SimpleNamespace(
            id=job["id"], user_id=uid, payload={"diary_id": str(did)},
        ))
        assert result.apply_domain is not None
        async with AsyncSession(db.engine) as session:
            await result.apply_domain(session)
            await session.commit()
    async with AsyncSession(db.engine) as session:
        recalled = await recall_diaries.recall(
            session, uid, query="semantic fixture", need="full", query_embedding=_vector(0), limit=5,
        )
    assert any(item["id"] == str(did) and item["content_match"] and item["body"] for item in recalled["items"])
    return embed.await_count


@pytest.mark.parametrize("mode", ["private", "dual", "shared"])
@pytest.mark.parametrize("language", ["ko", "ja", "en"])
async def test_real_welcome_publication_reuse_and_api_are_preserved(shared_db, monkeypatch, mode, language):
    db = shared_db
    db.state.mode = mode
    uid, other = await db.user(language), await db.user(language)
    profile = await _profile(db, uid)
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        did = await diary.ensure_welcome(session, profile)
        await session.commit()
        assert did is not None
        assert await diary.ensure_welcome(session, profile) is None
    row = await db.conn.fetchrow(
        "SELECT kind,source,title,content,activity_date,display_date,published_at,preset_ment_id "
        "FROM diaries WHERE user_id=$1 AND id=$2", uid, did,
    )
    title, body = diary._welcome_parts(language)
    assert row["kind"] == "welcome" and row["source"] == "welcome"
    assert (row["title"], row["content"]) == (title, body)
    assert row["activity_date"] is None and row["preset_ment_id"] is None
    assert row["display_date"] == diary._welcome_date(profile.created_at, profile.timezone)
    assert row["published_at"] == profile.created_at
    before = await _detail(db, uid, did)
    assert await _owned_embed_count(db, uid, did) == (0 if mode == "shared" else 1)
    assert await _finish_fixture_embeddings(db, uid, did, monkeypatch) == 1
    assert await _detail(db, uid, did) == before
    # A second real welcome reuses a ready approved canonical in dual/shared;
    # it must not lose its original diary identity or enqueue private work.
    second_profile = await _profile(db, other)
    async with AsyncSession(db.engine) as session:
        second = await diary.ensure_welcome(session, second_profile)
        await session.commit()
    assert second is not None and second != did
    assert await _owned_embed_count(db, other, second) == (1 if mode == "private" else 0)
    assert await _finish_fixture_embeddings(db, other, second, monkeypatch) == (1 if mode == "private" else 0)
    second_detail = await _detail(db, other, second)
    assert second_detail["body"] == before["body"] and second_detail["title"] == before["title"]
    async with AsyncSession(db.engine) as session:
        listing = await diary.list_diaries(session, str(other))
        assert [item["id"] for item in listing["data"]] == [str(second)]
    db.provider.assert_not_awaited()


_GENERATION_BODIES = {
    "ko": "오늘은 공원에서 귤을 먹으며 쉬었다.",
    "ja": "今日は公園でみかんを食べて休んだ。",
    "en": "I rested in the park and ate a mandarin.",
}


def _generation_week():
    # Historical and isolated from the current worker's weekly publication.
    return date(1980, 1, 7) + timedelta(weeks=uuid.uuid4().int % 500)


async def _generate(db, profile, day, week):
    async with AsyncSession(db.engine, expire_on_commit=False) as session:
        return await diary_generation.generate_for_user(
            session, profile, day,
            {"diary_min_user_chars": 1, "free_launch_until": "9999-12-31T00:00:00+00:00"},
            policy=diary_generation.DiaryPolicy(weekly_start_date=week),
        )


@pytest.mark.parametrize("mode", ["private", "dual", "shared"])
@pytest.mark.parametrize("language", ["ko", "ja", "en"])
async def test_real_operator_generation_translation_receipt_recall_and_push_selection(shared_db, monkeypatch, mode, language):
    from app.services import notify

    db = shared_db
    db.state.mode = mode
    week = _generation_week()
    original, translated = _GENERATION_BODIES["ko"], _GENERATION_BODIES[language]
    mid = await db.ment(original, week_override=week)
    uid = await db.user(language)
    profile = await _profile(db, uid)
    calls = []

    async def translate(system, convo, **kwargs):
        assert kwargs["ledger"].purpose == "diary_translate"
        calls.append("translation")
        return LLMResult(translated, 1, 1)

    monkeypatch.setattr(diary_generation.llm, "generate", translate)
    result = await _generate(db, profile, week, week)
    assert result["created"] and result["source"] == "preset" and not result["personal_attempted"]
    did = uuid.UUID(result["diary_id"])
    row = await db.conn.fetchrow("SELECT * FROM diaries WHERE user_id=$1 AND id=$2", uid, did)
    assert row["kind"] == "capi_day" and row["source"] == "preset"
    assert row["content"] == translated and row["title"] is None
    assert row["activity_date"] == row["display_date"] == week
    assert row["published_at"] == diary_generation.publish_at(week, profile.timezone)
    assert row["preset_ment_id"] == mid
    receipt = await db.conn.fetchrow(
        "SELECT status,preset_ment_id FROM diary_generation_results WHERE user_id=$1 AND target_date=$2", uid, week,
    )
    assert receipt["status"] == "preset" and receipt["preset_ment_id"] == mid
    assert calls == ([] if language == "ko" else ["translation"])
    before = await _detail(db, uid, did)
    assert before["body"] == translated and before["diary_date"] == week.isoformat()
    projection = await _projection(db, uid, did)
    # Translated historical bodies are not silently pooled with the Korean
    # operator source. Their existing private path remains intact in every mode.
    shared_approved = language == "ko" and mode != "private"
    assert bool(projection["shared_embedding_id"]) is shared_approved
    assert await _owned_embed_count(db, uid, did) == (0 if language == "ko" and mode == "shared" else 1)
    notification_time = row["published_at"] + timedelta(hours=1)
    async with AsyncSession(db.engine) as session:
        pending_selection = await notify._morning_diary_id(session, profile, notification_time)
    assert pending_selection == did
    assert await _finish_fixture_embeddings(db, uid, did, monkeypatch) == 1
    assert await _detail(db, uid, did) == before
    async with AsyncSession(db.engine) as session:
        assert await notify._morning_diary_id(session, profile, notification_time) == pending_selection
    repeat = await _generate(db, profile, week, week)
    assert repeat["reason"] == "already_exists" and not repeat["created"]
    assert await db.conn.fetchval("SELECT count(*) FROM diaries WHERE user_id=$1", uid) == 1
    assert calls == ([] if language == "ko" else ["translation"])
    db.provider.assert_not_awaited()


@pytest.mark.parametrize("mode", ["private", "dual", "shared"])
@pytest.mark.parametrize("language", ["ko", "ja", "en"])
async def test_real_personal_generation_provenance_and_api_remain_private(shared_db, monkeypatch, mode, language):
    from app.services import notify

    db = shared_db
    db.state.mode = mode
    week = _generation_week()
    uid = await db.user(language)
    profile = await _profile(db, uid)
    body = _GENERATION_BODIES[language]
    message = await db.conn.fetchval(
        "INSERT INTO messages(user_id,sender,content,activity_date) VALUES($1,'user',$2,$3) RETURNING id", uid, body, week,
    )
    calls = []

    async def generate(system, convo, **kwargs):
        purpose = kwargs["ledger"].purpose
        calls.append(purpose)
        if purpose == "diary_generate":
            return LLMResult("Weather: sunny\n" + body, 1, 1)
        assert purpose == "diary_self_check"
        return LLMResult("OK", 1, 1)

    monkeypatch.setattr(diary_generation.llm, "generate", generate)
    result = await _generate(db, profile, week, week)
    assert result["created"] and result["source"] == "llm" and result["personal_attempted"]
    did = uuid.UUID(result["diary_id"])
    row = await db.conn.fetchrow("SELECT * FROM diaries WHERE user_id=$1 AND id=$2", uid, did)
    assert row["kind"] == "shared_day" and row["source"] == "llm"
    assert row["content"] == body and row["preset_ment_id"] is None
    assert row["activity_date"] == row["display_date"] == week
    assert row["published_at"] == diary_generation.publish_at(week, profile.timezone)
    source = await db.conn.fetchrow("SELECT message_id,source_hash FROM diary_claim_sources WHERE user_id=$1 AND diary_id=$2", uid, did)
    assert source["message_id"] == message
    assert source["source_hash"] == hashlib.sha256(body.encode()).hexdigest()
    assert (await _projection(db, uid, did))["shared_embedding_id"] is None
    assert await _owned_embed_count(db, uid, did) == 1
    assert await db.conn.fetchval("SELECT count(*) FROM diary_generation_results WHERE user_id=$1", uid) == 0
    before = await _detail(db, uid, did)
    assert before["type"] == "personal" and before["body"] == body
    assert before["conversation_ref"] == {"anchor_date": week.isoformat()}
    notification_time = row["published_at"] + timedelta(hours=1)
    async with AsyncSession(db.engine) as session:
        assert await notify._morning_diary_id(session, profile, notification_time) == did
    assert await _finish_fixture_embeddings(db, uid, did, monkeypatch) == 1
    assert await _detail(db, uid, did) == before
    async with AsyncSession(db.engine) as session:
        assert await notify._morning_diary_id(session, profile, notification_time) == did
    assert (await _projection(db, uid, did))["shared_embedding_id"] is None
    assert await db.conn.fetchval("SELECT count(*) FROM diary_template_embeddings WHERE embedding_model=$1", db.model) == 0
    assert calls == ["diary_generate", "diary_self_check"]
    assert (await _generate(db, profile, week, week))["reason"] == "already_exists"
    assert calls == ["diary_generate", "diary_self_check"]
    db.provider.assert_not_awaited()
