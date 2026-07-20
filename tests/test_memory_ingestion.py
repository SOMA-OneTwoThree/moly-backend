"""장기기억 일별 ingestion — 일기 분리·watermark·재시도·출처 metadata."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.config import Settings
from app.models.memory_ingestion_state import MemoryIngestionState
from app.services import memory
from app.services import memory_ingestion as ingestion
from worker import tick

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
USER_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def _enable_ingestion(monkeypatch):
    monkeypatch.setattr(ingestion.settings, "memory_ingestion_enabled", True)


class _Session:
    def __init__(self):
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _claim(*, through=0, target=2, attempts=1):
    state = MemoryIngestionState(
        user_id=USER_ID,
        activity_date=date(2026, 7, 19),
        through_message_id=through,
        attempt_count=attempts,
        last_attempted_at=NOW,
    )
    return ingestion.IngestionClaim(state=state, target_message_id=target)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("memory_recall_rollout_percent", 101),
        ("memory_recall_search_top_k", 0),
        ("memory_ingestion_batch_size", -1),
        ("memory_ingestion_max_attempts", 0),
    ],
)
def test_memory_settings_reject_unsafe_ranges(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_existing_custom_memory_provider_names_remain_accepted():
    configured = Settings(
        _env_file=None,
        embedder_model="compatible-embedding-model",
        memory_collection="existing-production-collection",
    )

    assert configured.embedder_model == "compatible-embedding-model"
    assert configured.memory_collection == "existing-production-collection"


@pytest.mark.parametrize(
    "memory_options",
    [
        {},
        {"memory_ingestion_enabled": True},
        {"memory_recall_mode": "semantic", "memory_recall_rollout_percent": 1},
    ],
)
def test_production_requires_provider_config_for_memory(memory_options):
    configured = Settings(
        _env_file=None,
        environment="production",
        revenuecat_webhook_auth="configured",
        supabase_db_connection_string="",
        openai_api_key="",
        **memory_options,
    )

    with pytest.raises(RuntimeError, match="SUPABASE_DB_CONNECTION_STRING, OPENAI_API_KEY"):
        configured.require_production_ready()


def test_claim_uses_watermark_retry_cutoff_and_skip_locked():
    sql = str(
        ingestion._claim_statement(NOW).compile(dialect=postgresql.dialect())
    ).upper()
    assert "MAX(MESSAGES.ID)" not in sql
    assert "COMPLETED_AT IS NULL" in sql
    assert "ATTEMPT_COUNT" in sql
    assert "LAST_ATTEMPTED_AT" in sql
    assert "COALESCE" in sql
    assert "NOT (EXISTS" in sql
    assert "MEMORY_INGESTION_STATES_1.ACTIVITY_DATE < MEMORY_INGESTION_STATES.ACTIVITY_DATE" in sql
    assert "PROFILES.TIMEZONE" in sql
    assert "FOR UPDATE OF MEMORY_INGESTION_STATES SKIP LOCKED" in sql


async def test_claim_serializes_same_user_across_activity_dates():
    state = _claim(through=0, attempts=0).state

    class _ClaimResult:
        def scalar_one_or_none(self):
            return state

    class _ClaimSession(_Session):
        async def execute(self, statement, params=None):
            self.calls.append((str(statement), params))
            return _ClaimResult() if len(self.calls) == 1 else None

        async def scalar(self, statement, params=None):
            sql = str(statement)
            self.calls.append((sql, params))
            return True if "pg_try_advisory_xact_lock" in sql else 13

        async def flush(self):
            return None

    session = _ClaimSession()

    claim = await ingestion._claim_next(session, NOW)

    assert claim is not None and claim.target_message_id == 13
    assert "pg_try_advisory_xact_lock(hashtextextended(:u, 1))" in session.calls[1][0]
    assert session.calls[1][1] == {"u": USER_ID}


async def test_claim_skips_busy_user_instead_of_waiting():
    busy_state = _claim(through=0, attempts=0).state
    available_user_id = "22222222-2222-4222-8222-222222222222"
    available_state = MemoryIngestionState(
        user_id=available_user_id,
        activity_date=date(2026, 7, 19),
        through_message_id=0,
        attempt_count=0,
    )
    states = iter([busy_state, available_state])

    class _ClaimResult:
        def __init__(self, state):
            self.state = state

        def scalar_one_or_none(self):
            return self.state

    class _ClaimSession(_Session):
        def __init__(self):
            super().__init__()
            self.claim_statements = []
            self.locked_users = []

        async def execute(self, statement, params=None):
            self.claim_statements.append(statement)
            return _ClaimResult(next(states))

        async def scalar(self, statement, params=None):
            sql = str(statement)
            if "pg_try_advisory_xact_lock" in sql:
                self.locked_users.append(params["u"])
                return params["u"] == available_user_id
            return 23

        async def flush(self):
            return None

    session = _ClaimSession()

    claim = await ingestion._claim_next(session, NOW)

    assert claim is not None and str(claim.state.user_id) == available_user_id
    assert claim.target_message_id == 23
    assert session.locked_users == [USER_ID, available_user_id]
    assert session.rollbacks == 1
    retry_sql = str(
        session.claim_statements[1].compile(dialect=postgresql.dialect())
    ).upper()
    assert "MEMORY_INGESTION_STATES.USER_ID NOT IN" in retry_sql


async def test_late_messages_advance_watermark_and_attach_source_metadata(monkeypatch):
    claim = _claim(through=10, target=13)
    messages = [
        SimpleNamespace(id=12, sender="user", content="요즘 수영을 시작했어"),
        SimpleNamespace(id=13, sender="moly", content="즐겁게 다녀와!"),
    ]
    captured = {}

    async def _messages(session, got_claim):
        assert got_claim is claim
        return messages

    async def _add(user_id, payload, *, metadata=None):
        captured.update(user_id=user_id, payload=payload, metadata=metadata)
        return {"results": []}

    monkeypatch.setattr(ingestion, "_messages_for_claim", _messages)
    monkeypatch.setattr(memory, "add_conversation", _add)
    session = _Session()

    await ingestion._process_claim(session, claim, NOW)

    assert captured == {
        "user_id": USER_ID,
        "payload": [
            {"role": "user", "content": "요즘 수영을 시작했어"},
            {"role": "assistant", "content": "즐겁게 다녀와!"},
        ],
        "metadata": {
            "source": "conversation",
            "schema_version": 1,
            "attributed_to": "user",
            "activity_date": "2026-07-19",
            "message_id_start": 12,
            "message_id_end": 13,
            "ingestion_key": "v1:2026-07-19:12:13",
        },
    }
    assert claim.state.through_message_id == 13
    assert claim.state.completed_at == NOW
    assert session.calls == []


async def test_claim_processing_has_end_to_end_timeout(monkeypatch):
    claim = _claim()
    messages = [SimpleNamespace(id=1, sender="user", content="수영을 시작했어")]

    async def _messages(session, got_claim):
        return messages

    async def _slow_add(*args, **kwargs):
        await asyncio.sleep(0.05)

    monkeypatch.setattr(ingestion, "_messages_for_claim", _messages)
    monkeypatch.setattr(memory, "add_conversation", _slow_add)
    monkeypatch.setattr(ingestion.settings, "memory_ingestion_timeout_seconds", 0.001)

    with pytest.raises(TimeoutError):
        await ingestion._process_claim(_Session(), claim, NOW)

    assert claim.state.through_message_id == 0
    assert claim.state.completed_at is None


async def test_mem0_failure_is_committed_for_hourly_retry_and_does_not_stop_batch(monkeypatch):
    claim = _claim()
    claims = iter([claim, None])

    async def _next(session, now):
        return next(claims)

    async def _fail(session, got_claim, now):
        raise RuntimeError("mem0 unavailable")

    monkeypatch.setattr(ingestion, "_claim_next", _next)
    monkeypatch.setattr(ingestion, "_process_claim", _fail)
    session = _Session()

    assert await ingestion.ingest_pending(session, NOW, batch_size=2) == 0
    assert session.commits == 1
    assert session.rollbacks == 1


async def test_success_refreshes_snapshot_before_releasing_user_lock(monkeypatch):
    claim = _claim()
    claims = iter([claim, None])
    order = []

    async def _next(session, now):
        return next(claims)

    async def _process(session, got_claim, now):
        order.append("process")

    async def _refresh(session, user_id, now):
        assert session.commits == 0
        assert now == NOW
        order.append("refresh")

    monkeypatch.setattr(ingestion, "_claim_next", _next)
    monkeypatch.setattr(ingestion, "_process_claim", _process)
    monkeypatch.setattr(ingestion, "_refresh_legacy_snapshot", _refresh)
    session = _Session()

    assert await ingestion.ingest_pending(session, NOW, batch_size=2) == 1
    assert order == ["process", "refresh"]
    assert session.commits == 1
    assert session.calls == [
        (
            "SELECT pg_advisory_xact_lock(hashtextextended(:u, 0))",
            {"u": USER_ID},
        )
    ]


async def test_snapshot_provider_failure_marks_snapshot_stale_and_commits(monkeypatch):
    claim = _claim()
    claims = iter([claim, None])
    marked = []

    async def _next(session, now):
        return next(claims)

    async def _process(session, got_claim, now):
        got_claim.state.completed_at = now

    async def _refresh(session, user_id, now):
        raise memory.MemoryUnavailable("provider down")

    async def _mark_stale(session, user_id, now):
        marked.append((user_id, now))

    monkeypatch.setattr(ingestion, "_claim_next", _next)
    monkeypatch.setattr(ingestion, "_process_claim", _process)
    monkeypatch.setattr(ingestion, "_refresh_legacy_snapshot", _refresh)
    monkeypatch.setattr(ingestion, "_mark_legacy_snapshot_stale", _mark_stale)
    session = _Session()

    assert await ingestion.ingest_pending(session, NOW, batch_size=2) == 1
    assert marked == [(USER_ID, NOW)]
    assert session.commits == 1


async def test_post_write_commit_failure_stops_tick_before_duplicate_add(monkeypatch):
    claim = _claim()
    process_calls = 0

    async def _next(session, now):
        return claim

    async def _process(session, got_claim, now):
        nonlocal process_calls
        process_calls += 1

    async def _refresh(session, user_id, now):
        return None

    class _CommitFailureSession(_Session):
        async def commit(self):
            self.commits += 1
            raise RuntimeError("connection lost after mem0 write")

    monkeypatch.setattr(ingestion, "_claim_next", _next)
    monkeypatch.setattr(ingestion, "_process_claim", _process)
    monkeypatch.setattr(ingestion, "_refresh_legacy_snapshot", _refresh)
    session = _CommitFailureSession()

    assert await ingestion.ingest_pending(session, NOW, batch_size=50) == 0
    assert process_calls == 1
    assert session.commits == 1
    assert session.rollbacks == 1


async def test_refresh_legacy_snapshot_upserts_authoritative_value(monkeypatch):
    async def _load(user_id):
        return "- 고양이를 키움"

    monkeypatch.setattr(memory, "load_for_context", _load)
    session = _Session()

    await ingestion._refresh_legacy_snapshot(session, USER_ID, NOW)

    sql, params = session.calls[0]
    assert "INSERT INTO chat_contexts" in sql
    assert "ON CONFLICT (user_id) DO UPDATE" in sql
    assert params == {
        "u": USER_ID,
        "memory": "- 고양이를 키움",
        "refreshed_at": NOW,
    }


async def test_mark_legacy_snapshot_stale_preserves_fallback_timestamp():
    session = _Session()

    await ingestion._mark_legacy_snapshot_stale(session, USER_ID, NOW)

    sql, params = session.calls[0]
    assert "LEAST(memory_refreshed_at, :stale_before)" in sql
    assert params == {
        "u": USER_ID,
        "stale_before": NOW
        - timedelta(hours=ingestion.settings.memory_snapshot_refresh_hours),
    }


def test_migration_seeds_existing_messages_without_backfill_extraction():
    root = Path(__file__).parents[1]
    migration = (
        root / "db/migrations/20260720_memory_ingestion_states.sql"
    ).read_text(encoding="utf-8")
    seed = (
        root / "db/migrations/20260720_memory_ingestion_states_seed.sql"
    ).read_text(encoding="utf-8")
    schema = (root / "db/schema.sql").read_text(encoding="utf-8")

    assert "THEN max(m.id)" in seed
    assert "ELSE 0" in seed
    assert "JOIN public.profiles p ON p.id = m.user_id" in seed
    assert "m.activity_date < ((now() AT TIME ZONE p.timezone)" in seed
    assert seed.count("d.user_id = m.user_id") == 2
    assert seed.count("d.diary_date = m.activity_date") == 2
    assert seed.count("d.source IN ('llm', 'preset')") == 2
    assert "ON CONFLICT (user_id, activity_date) DO NOTHING" in seed
    assert "CREATE TRIGGER" not in seed
    assert "messages_mark_memory_ingestion_pending" in migration
    assert "WHEN (NEW.kind = 'normal' AND NEW.sender = 'moly')" in migration
    assert "attempt_count = 0" in migration
    assert "memory_ingestion_pending_idx" in migration
    assert "CREATE INDEX IF NOT EXISTS messages_memory_ingestion_idx" not in migration
    assert "SELECT\n  m.user_id" not in migration
    schema_table = schema.split("CREATE TABLE public.memory_ingestion_states", 1)[1].split(
        "CREATE TABLE public.idempotency_keys", 1
    )[0]
    assert "SELECT m.user_id, m.activity_date" not in schema_table
    assert "messages_mark_memory_ingestion_pending" in schema_table
    assert "attempt_count = 0" in schema_table


async def test_add_conversation_uses_user_grounded_instruction_and_metadata(monkeypatch):
    captured = {}

    class _Memory:
        async def add(self, messages, **kwargs):
            captured.update(messages=messages, **kwargs)

    monkeypatch.setattr(memory, "_get_memory", lambda: _Memory())
    metadata = {"activity_date": "2026-07-19", "source": "conversation"}

    await memory.add_conversation(
        USER_ID,
        [{"role": "user", "content": "나는 민초를 싫어해"}],
        metadata=metadata,
    )

    assert captured["user_id"] == USER_ID
    assert captured["metadata"] == metadata
    assert 'attributed_to must be "user"' in captured["prompt"]
    assert "Assistant messages are context only" in captured["prompt"]
    assert "2026-07-19 as the observation date" in captured["prompt"]


async def test_add_conversation_verifies_returned_vectors(monkeypatch):
    fetched = []

    class _Memory:
        async def add(self, messages, **kwargs):
            return {"results": [{"id": "memory-1", "memory": "수영을 시작함"}]}

        async def get(self, memory_id):
            fetched.append(memory_id)
            return {"id": memory_id, "user_id": USER_ID}

    monkeypatch.setattr(memory, "_get_memory", lambda: _Memory())

    count = await memory.add_conversation(
        USER_ID,
        [{"role": "user", "content": "수영을 시작했어"}],
    )

    assert count == 1
    assert fetched == ["memory-1"]


async def test_add_conversation_rejects_mem0_false_success(monkeypatch):
    class _Memory:
        async def add(self, messages, **kwargs):
            return {"results": [{"id": "missing-memory", "memory": "저장 실패"}]}

        async def get(self, memory_id):
            return None

    monkeypatch.setattr(memory, "_get_memory", lambda: _Memory())

    with pytest.raises(memory.MemoryWriteUnavailable):
        await memory.add_conversation(
            USER_ID,
            [{"role": "user", "content": "요즘 수영을 시작했어"}],
        )


@pytest.mark.parametrize(
    "response",
    [
        "",
        "not json",
        '{"memories": []}',
        '{"memory": [{"text": "출처가 없음"}]}',
    ],
)
def test_extraction_response_rejects_ambiguous_or_assistant_memory(response):
    with pytest.raises(memory.MemoryWriteUnavailable):
        memory._validate_extraction_response(response)


def test_extraction_response_accepts_explicit_zero_facts():
    memory._validate_extraction_response('{"memory": []}')


def test_extraction_response_drops_assistant_items_but_keeps_user_facts():
    normalized = memory._normalize_extraction_response(
        '{"memory": ['
        '{"id":"0","text":"사용자는 수영을 시작함","attributed_to":"user"},'
        '{"id":"1","text":"캐피는 등산을 좋아함","attributed_to":"assistant"}'
        "]}"
    )

    assert normalized == (
        '{"memory": [{"id": "0", "text": "사용자는 수영을 시작함", '
        '"attributed_to": "user"}]}'
    )


async def test_add_conversation_surfaces_swallowed_embedding_failure(monkeypatch):
    class _Memory:
        async def add(self, messages, **kwargs):
            import logging

            logging.getLogger("mem0.memory.main").warning(
                "Failed to embed memory text (async): provider down"
            )
            return {"results": []}

    monkeypatch.setattr(memory, "_get_memory", lambda: _Memory())

    with pytest.raises(memory.MemoryWriteUnavailable, match="swallowed"):
        await memory.add_conversation(
            USER_ID,
            [{"role": "user", "content": "수영을 시작했어"}],
        )


def test_mem0_config_keeps_history_in_memory(monkeypatch):
    captured = {}
    instance = object()

    class _AsyncMemory:
        @classmethod
        def from_config(cls, config):
            captured.update(config)
            return instance

    monkeypatch.setitem(sys.modules, "mem0", SimpleNamespace(AsyncMemory=_AsyncMemory))
    monkeypatch.setattr(memory, "_memory", None)

    assert memory._get_memory() is instance
    assert captured["history_db_path"] == ":memory:"
    assert captured["custom_instructions"] == memory.MEMORY_EXTRACTION_INSTRUCTIONS


def test_mem0_config_bounds_openai_calls_and_disables_sampling(monkeypatch):
    captured = {}
    option_calls = []

    class _ProviderClient:
        def with_options(self, **kwargs):
            option_calls.append(kwargs)
            return SimpleNamespace(options=kwargs)

    instance = SimpleNamespace(
        llm=SimpleNamespace(client=_ProviderClient()),
        embedding_model=SimpleNamespace(client=_ProviderClient()),
    )

    class _AsyncMemory:
        @classmethod
        def from_config(cls, config):
            captured.update(config)
            return instance

    monkeypatch.setitem(sys.modules, "mem0", SimpleNamespace(AsyncMemory=_AsyncMemory))

    assert memory._create_memory() is instance
    assert captured["llm"]["config"]["temperature"] == 0.0
    assert option_calls == [
        {
            "timeout": memory.settings.memory_provider_timeout_seconds,
            "max_retries": memory.settings.memory_provider_max_retries,
        },
        {
            "timeout": memory.settings.memory_provider_timeout_seconds,
            "max_retries": memory.settings.memory_provider_max_retries,
        },
    ]


async def test_worker_runs_ingestion_on_every_hour(monkeypatch):
    class _Rows:
        def scalars(self):
            return self

        def all(self):
            return []

    class _WorkerSession(_Session):
        async def execute(self, statement, params=None):
            return _Rows()

    class _Context:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, exc_type, exc, tb):
            return None

    called = []
    session = _WorkerSession()

    async def _config(got_session):
        return {}

    async def _ingest(got_session, now):
        called.append((got_session, now))
        return 3

    monkeypatch.setattr(tick, "get_sessionmaker", lambda: lambda: _Context(session))
    monkeypatch.setattr(tick, "effective_token_config", _config)
    monkeypatch.setattr(tick.memory_ingestion, "ingest_pending", _ingest)

    counts = await tick.run_tick(NOW)

    assert called == [(session, NOW)]
    assert counts["memory_ingestions"] == 3


async def test_worker_fails_before_claiming_when_memory_provider_is_missing(monkeypatch):
    monkeypatch.setattr(tick.settings, "memory_ingestion_enabled", True)
    monkeypatch.setattr(tick.settings, "supabase_db_connection_string", "")
    monkeypatch.setattr(tick.settings, "openai_api_key", "")
    monkeypatch.setattr(
        tick,
        "get_sessionmaker",
        lambda: (_ for _ in ()).throw(AssertionError("worker opened a DB session")),
    )

    with pytest.raises(RuntimeError, match="SUPABASE_DB_CONNECTION_STRING, OPENAI_API_KEY"):
        await tick.run_tick(NOW)


async def test_worker_surfaces_global_ingestion_schema_failure(monkeypatch):
    class _Rows:
        def scalars(self):
            return self

        def all(self):
            return []

    session = _Session()

    class _Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return None

    async def _config(got_session):
        return {}

    async def _fail(got_session, now):
        raise RuntimeError("memory_ingestion_states missing")

    async def _execute(statement, params=None):
        return _Rows()

    session.execute = _execute
    monkeypatch.setattr(tick, "get_sessionmaker", lambda: lambda: _Context())
    monkeypatch.setattr(tick, "effective_token_config", _config)
    monkeypatch.setattr(tick.memory_ingestion, "ingest_pending", _fail)

    with pytest.raises(RuntimeError, match="memory_ingestion_states missing"):
        await tick.run_tick(NOW)

    assert session.rollbacks == 1


async def test_worker_skips_ingestion_when_kill_switch_is_off(monkeypatch):
    monkeypatch.setattr(tick.settings, "memory_ingestion_enabled", False)

    class _Rows:
        def scalars(self):
            return self

        def all(self):
            return []

    class _WorkerSession(_Session):
        async def execute(self, statement, params=None):
            return _Rows()

    class _Context:
        async def __aenter__(self):
            return _WorkerSession()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    async def _must_not_run(*args, **kwargs):
        raise AssertionError("ingestion kill switch ignored")

    async def _config(session):
        return {}

    monkeypatch.setattr(tick, "get_sessionmaker", lambda: lambda: _Context())
    monkeypatch.setattr(tick, "effective_token_config", _config)
    monkeypatch.setattr(tick.memory_ingestion, "ingest_pending", _must_not_run)

    counts = await tick.run_tick(NOW)

    assert counts["memory_ingestions"] == 0
