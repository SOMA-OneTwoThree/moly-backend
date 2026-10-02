"""Usage-ledger rollup executed by the real handler against a disposable local DB.

The unit tests pin the SQL text; these run it. Every handler session shares one
transaction (commit = flush) that is rolled back afterwards.
"""
from __future__ import annotations

import os
import uuid
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from db.schema_contract import require_scratch
from worker import retention_jobs as rt

_KST = ZoneInfo('Asia/Seoul')
_SUMS = ('input_tokens', 'cached_input_tokens', 'cache_write_tokens',
         'output_tokens', 'embedding_tokens', 'cost_micro_usd')
_COLS = ('calls',) + _SUMS
_INSERT = text("""
INSERT INTO ai_usage_ledger
  (lane, purpose, provider, model, status, started_at, price_catalog_version,
   input_tokens, cached_input_tokens, cache_write_tokens, output_tokens, embedding_tokens,
   cost_micro_usd)
VALUES (:lane, :purpose, 'openai', :model, :status, :started_at, :price,
        :input_tokens, :cached_input_tokens, :cache_write_tokens, :output_tokens,
        :embedding_tokens, :cost_micro_usd)
""")
_ROLLED_BEFORE = text(
    'INSERT INTO ai_usage_daily_rollup (kst_date, provider, model, lane, purpose, status, '
    + ', '.join(_COLS) + ") VALUES (:kst_date, 'openai', :model, :lane, :purpose, :status, "
    + ', '.join(f':{c}' for c in _COLS) + ')'
)


class _OneTransaction:
    def __init__(self, session: AsyncSession):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    async def commit(self) -> None:
        await self._session.flush()


@pytest_asyncio.fixture
async def ledger(monkeypatch):
    dsn = os.environ.get('MOLY_SCHEMA_TEST_DSN')
    if not dsn:
        pytest.skip('MOLY_SCHEMA_TEST_DSN required')
    require_scratch(dsn)
    # Same driver settings as the app engine: the handler stops on asyncpg's rowcount.
    engine = create_async_engine(dsn.replace('postgresql://', 'postgresql+asyncpg://'),
                                 poolclass=NullPool, connect_args={'statement_cache_size': 0})
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            shared = _OneTransaction(session)

            @asynccontextmanager
            async def handler_session():
                yield shared

            monkeypatch.setattr(rt, 'get_sessionmaker', lambda: handler_session)
            yield session
            await session.rollback()
    finally:
        await engine.dispose()


async def _cutoff(session) -> datetime:
    """KST midnight 90 days before KST today, from the DB clock and independent of the SQL."""
    now = await session.scalar(text('SELECT now()'))
    return datetime.combine(now.astimezone(_KST).date() - timedelta(days=90), time(0),
                            tzinfo=_KST)


def _row(model, started_at, n, *, status='completed', lane='foreground', purpose='tool_decide'):
    return {
        'model': model, 'started_at': started_at, 'status': status, 'lane': lane,
        'purpose': purpose, 'price': 1 if status == 'completed' else None,
        'input_tokens': n % 1000, 'cached_input_tokens': n % 500, 'cache_write_tokens': n % 100,
        'output_tokens': n % 300, 'embedding_tokens': n % 50,
        'cost_micro_usd': None if n % 7 == 0 else n * 3,  # NULL cost is summed as 0
    }


def _old_finished(model, cutoff, count):
    """Finished rows spread over the two KST days before the cutoff, in several groups."""
    rows = []
    for n in range(1, count + 1):
        day = cutoff - timedelta(days=1 + (n // 2) % 2)
        rows.append(_row(model, day + timedelta(seconds=(n * 997) % 86400), n,
                         status='failed' if n % 11 == 0 else 'completed',
                         lane=('foreground', 'background')[n % 2],
                         purpose=('tool_decide', 'memory_extract', 'diary_generate')[n % 3]))
    return rows


def _expected(rows, cutoff):
    out = defaultdict(Counter)
    for r in rows:
        if r['status'] in ('completed', 'failed') and r['started_at'] < cutoff:
            kst_date = r['started_at'].astimezone(_KST).date()
            group = out[(kst_date, r['lane'], r['purpose'], r['status'])]
            group['calls'] += 1
            for col in _SUMS:
                group[col] += r[col] or 0
    return {key: dict(group) for key, group in out.items()}


async def _totals(session, model):
    result = await session.execute(text(
        'SELECT kst_date, lane, purpose, status, ' + ', '.join(_COLS)
        + ' FROM ai_usage_daily_rollup WHERE model = :model'), {'model': model})
    return {(r['kst_date'], r['lane'], r['purpose'], r['status']): {c: r[c] for c in _COLS}
            for r in result.mappings()}


async def _run():
    return await rt.handle_usage_rollup(SimpleNamespace(payload={}))


async def test_old_finished_rows_fold_into_kst_daily_totals_exactly_once(ledger):
    session = ledger
    model = f'rollup-test-{uuid.uuid4().hex[:8]}'
    cutoff = await _cutoff(session)
    day_before = (cutoff - timedelta(days=1)).date()
    two_days_before = (cutoff - timedelta(days=2)).date()
    rolled = _old_finished(model, cutoff, 240) + [
        # One second apart, same UTC date, different KST dates.
        _row(model, cutoff - timedelta(days=1), 1, purpose='context_summary'),
        _row(model, cutoff - timedelta(days=1, seconds=1), 2, purpose='context_summary'),
        _row(model, cutoff - timedelta(microseconds=1), 3, purpose='boundary'),
        # A group with no cost at all still totals 0, never NULL.
        *(dict(_row(model, cutoff - timedelta(hours=n), n, status='failed', purpose='unpriced'),
               cost_micro_usd=None) for n in (1, 2)),
    ]
    kept = [
        _row(model, cutoff, 4, purpose='boundary'),
        _row(model, cutoff + timedelta(days=80), 5),
        *(_row(model, cutoff - timedelta(days=30), n, status=status, lane='background',
               purpose='memory_extract')
          for n, status in enumerate(['started'] * 3 + ['unknown_usage'] * 4)),
    ]
    await session.execute(_INSERT, rolled + kept)
    # A group rolled on an earlier day is added to, not overwritten.
    earlier = {'calls': 5, 'input_tokens': 100, 'cached_input_tokens': 40,
               'cache_write_tokens': 7, 'output_tokens': 60, 'embedding_tokens': 0,
               'cost_micro_usd': 1000}
    await session.execute(_ROLLED_BEFORE, {
        'kst_date': day_before, 'model': model, 'lane': 'foreground', 'purpose': 'tool_decide',
        'status': 'completed', **earlier})
    expected = _expected(rolled, cutoff)
    group = expected.setdefault((day_before, 'foreground', 'tool_decide', 'completed'),
                                dict.fromkeys(_COLS, 0))
    for col, value in earlier.items():
        group[col] += value

    result = await _run()

    assert result.result_detail['more'] is False and result.apply_domain is None
    assert result.result_detail['stale_started'] >= 3
    totals = await _totals(session, model)
    assert totals == expected
    assert {k[0]: v['calls'] for k, v in totals.items() if k[2] == 'context_summary'} == {
        day_before: 1, two_days_before: 1}
    left = (await session.execute(text(
        'SELECT status, started_at, purpose FROM ai_usage_ledger WHERE model = :model'),
        {'model': model})).all()
    assert Counter(r.status for r in left) == {'completed': 2, 'started': 3, 'unknown_usage': 4}
    assert {r.started_at for r in left if r.purpose == 'boundary'} == {cutoff}
    assert await session.scalar(text(
        "SELECT count(*) FROM ai_usage_ledger"
        " WHERE status IN ('completed','failed') AND started_at < :cutoff"),
        {'cutoff': cutoff}) == 0
    recorded, db_now = (await session.execute(text(
        "SELECT value #>> '{}', now() FROM app_config WHERE key = :key"),
        {'key': rt.config_store.RETENTION_LAST_SUCCESS_PREFIX + rt.JOB_USAGE_ROLLUP})).one()
    assert abs(datetime.fromisoformat(recorded) - db_now) < timedelta(seconds=60)


async def test_batches_continue_across_runs_and_a_rerun_changes_nothing(ledger, monkeypatch):
    session = ledger
    monkeypatch.setattr(rt, 'BATCH', 40)
    monkeypatch.setattr(rt, 'MAX_BATCHES', 3)
    model = f'rollup-test-{uuid.uuid4().hex[:8]}'
    cutoff = await _cutoff(session)
    rows = _old_finished(model, cutoff, 300)
    await session.execute(_INSERT, rows)
    expected = _expected(rows, cutoff)

    today = datetime.now(_KST).date().isoformat()  # the continuation chain's date key
    runs = [await _run()]
    while runs[-1].result_detail['more'] and len(runs) < 20:
        runs.append(await _run())

    assert runs[0].result_detail['more'] is True and runs[0].apply_domain is not None
    assert runs[-1].result_detail['more'] is False and runs[-1].apply_domain is None
    assert len(runs) == 3  # 120 rows per run: three batches of 40
    # Groups split across batches and runs still add up exactly once.
    assert await _totals(session, model) == expected
    # The consumer applies the continuation in its own transaction; here it is the test's.
    await runs[0].apply_domain(session)
    job = (await session.execute(text(
        'SELECT queue, priority, dedup_key, payload FROM async_jobs'
        " WHERE job_type = 'usage_ledger_rollup'"))).one()
    assert job.dedup_key == f'usage_ledger_rollup:{today}:1'
    assert job.payload == {'seq': 1, 'date_key': today}
    assert (job.queue, job.priority) == ('maintenance', 200)

    again = await _run()
    assert again.result_detail['rollup_rows'] == 0 and again.result_detail['more'] is False
    assert await _totals(session, model) == expected
