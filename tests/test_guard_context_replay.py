"""개발 리플레이 도구의 경계·설정 복구·privacy 정리. 네트워크/DB 호출 없음."""
from datetime import date, datetime, timezone
import asyncio
import json
from types import SimpleNamespace
import uuid

import pytest

from scripts import replay_guard_context_dev as replay


def private(tmp_path, name="corpus.json", data=None):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(data or {}))
    path.chmod(0o600)
    return path


def corpus():
    return {"schema": "guard-context-replay-v1", "redaction_reviewed": True,
        "scenarios": [{"number": 1, "language": "ko", "origin": "deidentified",
                       "steps": [{"text": "합성 테스트", "kind": "normal"}]}]}


def env():
    return {"SUPABASE_URL": replay.AUTH, "SUPABASE_DB_CONNECTION_STRING":
        f"postgresql://postgres.{replay.REF}:synthetic@aws-0-test.pooler.supabase.com:6543/postgres",
        "SUPABASE_SECRET_KEY": "synthetic-secret", "SUPABASE_ANON_KEY": "synthetic-public"}


def runner(db, client=None, emit=None):
    return replay.Runner(db, client, {"apikey": "synthetic-secret"}, "synthetic-public", "a"*40,
                         emit or (lambda row: None), recovery_timeout=0)


def state(users=()):
    return {"schema": "guard-context-replay-state-v1", "ref": replay.REF,
            "run": "synthetic-run", "users": list(users), "saved": {}, "written": {}}


def test_default_mode_validates_private_corpus_without_execution(tmp_path, monkeypatch, capsys):
    path = private(tmp_path, data=corpus())
    async def forbidden(*args, **kwargs):
        raise AssertionError("validation must not initialize clients")
    monkeypatch.setattr(replay, "execute", forbidden)
    monkeypatch.setattr(replay.logging, "disable", lambda level: None)
    assert replay.main(["--input", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["route"] == "validation" and output["failure"] is None
    assert "합성 테스트" not in json.dumps(output, ensure_ascii=False)


@pytest.mark.parametrize("mutation", [
    {"SUPABASE_URL": "https://production.example"},
    {"SUPABASE_DB_CONNECTION_STRING": "postgresql://postgres@localhost:5432/postgres"},
    {"SUPABASE_DB_CONNECTION_STRING": f"postgresql://postgres.{replay.REF}@evil.example:5432/postgres"},
    {"SUPABASE_DB_CONNECTION_STRING": "postgresql://postgres.wrong@aws-0-test.pooler.supabase.com:6543/postgres"},
    {"SUPABASE_DB_CONNECTION_STRING": f"postgresql://postgres.{replay.REF}@aws-0-test.pooler.supabase.com:6543/postgres?host=evil.example"},
])
def test_targets_reject_all_non_development_connections(mutation):
    values = env() | mutation
    with pytest.raises(replay.ReplayError, match="development_target_required"):
        replay.validate_targets(values, "a"*40)


def test_exact_development_target_and_domain_constants():
    assert replay.validate_targets(env(), "a"*40).startswith("postgresql://")
    assert replay.API == "https://dev.moly.asia"
    assert replay.AUTH == f"https://{replay.REF}.supabase.co"


@pytest.mark.parametrize("mode", [0o644, 0o660])
def test_shared_corpus_permissions_are_rejected(tmp_path, mode):
    path = private(tmp_path, data=corpus())
    path.chmod(mode)
    with pytest.raises(replay.ReplayError, match="private_file_permissions"):
        replay.load_corpus(path, checkout=tmp_path / "checkout")


def test_private_corpus_in_another_git_checkout_is_rejected(tmp_path):
    # Other worktrees are also Git data, even when not this script's checkout.
    other = tmp_path / "another-checkout"
    other.mkdir(mode=0o700)
    (other / ".git").write_text("gitdir: /synthetic/common/git/worktrees/other")
    path = private(other, data=corpus())
    with pytest.raises(replay.ReplayError, match="private_file_in_checkout"):
        replay.private_file(path, checkout=tmp_path / "current-checkout")


@pytest.mark.parametrize("value", ["synthetic@example.test", "Bearer synthetic", "sk-abcdefghijklmnop"])
def test_redaction_barrier_rejects_obvious_identifiers_without_echo(tmp_path, value):
    data = corpus()
    data["scenarios"][0]["steps"][0]["text"] = value
    with pytest.raises(replay.ReplayError, match="unredacted_identifier_detected") as error:
        replay.load_corpus(private(tmp_path, data=data), checkout=tmp_path / "checkout")
    assert value not in str(error.value)


def test_existing_guard_cohort_never_has_timeout_changed_for_replay():
    saved = {"crisis_guard_enabled": True, "crisis_guard_policy_version": replay.POLICY,
        "crisis_guard_timeout_s": 0.1, "crisis_guard_history_messages": 6,
        "crisis_guard_ready_users": [str(uuid.uuid4())]}
    try:
        patch = replay.config_patch(saved, [str(uuid.uuid4())])
    except replay.ReplayError:
        return  # Refusing a conflicting existing cohort is acceptable.
    assert patch["crisis_guard_timeout_s"] == saved["crisis_guard_timeout_s"]


def test_existing_disabled_cohort_is_rejected():
    with pytest.raises(replay.ReplayError, match="existing_cohort_would_be_enabled"):
        replay.config_patch({"context_retention_v2_enabled": False,
            "context_retention_ready_users": [str(uuid.uuid4())]}, [str(uuid.uuid4())])


class Tx:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass


async def test_disabled_retention_cannot_enable_unlisted_published_v5_user(tmp_path):
    stamp = datetime.now(timezone.utc)
    class DB:
        def transaction(self):
            return Tx()
        async def fetch(self, sql, *args):
            if "app_config" in sql:
                return [{"key": "context_retention_v2_enabled", "value": False, "updated_at": stamp}]
            if "conversation_checkpoints" in sql:
                return [{"user_id": uuid.uuid4()}]
            raise AssertionError("unexpected query")
        async def fetchval(self, sql, *args):
            if "conversation_checkpoints" in sql:
                return 1  # unrelated current-generation published v5 adopter
            if "transaction_timestamp" in sql:
                return stamp
            return args[0]
    journal = private(tmp_path, "run.state.json")
    with pytest.raises(replay.ReplayError):
        await runner(DB()).configure(state([str(uuid.uuid4())]), journal)


@pytest.fixture
def cleanup(monkeypatch, tmp_path):
    uid = uuid.uuid4()
    current = state()
    journal = private(tmp_path, "run.state.json", current)
    events, emitted = [], []
    flags = {"owned": True, "remaining": 0, "barrier": "deleted", "config": None}
    class DB:
        async def fetch(self, sql, *args):
            assert "raw_user_meta_data->>'guard_context_replay'=$1" in sql
            assert args == (current["run"],)
            events.append("recover_owned")
            return [{"id": uid}]
        async def execute(self, sql, *args):
            events.append(("config_cas", sql, args))
            assert "value=$2::jsonb AND updated_at=$3" in sql
            return "UPDATE 0"
        async def fetchval(self, sql, *args):
            if "begin_subject_deletion" in sql:
                assert args[0] == uid
                events.append("begin_deletion")
                return 0
            if "delete_user_memories" in sql:
                assert args == (uid,)
                events.append("delete_memories")
                return 0
            assert "auth.users WHERE id=$1" in sql and args == (uid,)
            events.append("auth_absent")
            return 0
        async def fetchrow(self, sql, *args):
            if "app_config" in sql:
                return flags["config"]
            assert args == (uid,)
            assert "vecs.moly_memories_v2" in sql and "async_jobs" in sql
            events.append("verify_remaining")
            return {"remaining": flags["remaining"], "state": flags["barrier"]}
    class Client:
        async def get(self, url, **kwargs):
            assert url == f"{replay.AUTH}/auth/v1/admin/users/{uid}"
            events.append("verify_owner")
            return SimpleNamespace(status_code=200, json=lambda: {"user_metadata": {
                "guard_context_replay": current["run"] if flags["owned"] else "other-run"}})
        async def delete(self, url, **kwargs):
            assert url == f"{replay.AUTH}/auth/v1/admin/users/{uid}"
            events.append("auth_delete")
            return SimpleNamespace(status_code=200)
    async def sleep(seconds):
        events.append("wait")
    monkeypatch.setattr(replay.asyncio, "sleep", sleep)
    return SimpleNamespace(uid=uid, state=current, journal=journal, events=events, emitted=emitted,
                           flags=flags, runner=runner(DB(), Client(), emitted.append))


async def test_cleanup_recovers_lost_creation_response_and_fences_before_exact_auth_delete(cleanup):
    e = cleanup
    assert await e.runner.cleanup(e.state, e.journal)
    assert e.state["users"] == [str(e.uid)]
    assert e.events.index("begin_deletion") < e.events.index("auth_delete") < e.events.index("delete_memories")
    assert e.events.count("verify_remaining") == 2 and not e.journal.exists()
    assert e.emitted == [replay.report(0, "none", "cleanup", 0, None)]


async def test_cleanup_owner_mismatch_cannot_delete_and_keeps_journal(cleanup):
    e = cleanup
    e.flags["owned"] = False
    assert not await e.runner.cleanup(e.state, e.journal)
    assert "auth_delete" not in e.events and "begin_deletion" not in e.events
    assert "delete_memories" not in e.events and e.journal.exists()
    assert e.emitted[0]["failure"] == "cleanup_incomplete"


async def test_restore_cas_cannot_overwrite_concurrent_config_edit(cleanup):
    e = cleanup
    stamp = datetime.now(timezone.utc).isoformat()
    e.state["saved"] = {"crisis_guard_enabled": {"value": False, "updated_at": stamp}}
    e.state["written"] = {"crisis_guard_enabled": {"value": True, "updated_at": stamp}}
    e.flags["config"] = {"value": "concurrently_changed"}
    assert not await e.runner.cleanup(e.state, e.journal)
    updates = [event for event in e.events if isinstance(event, tuple) and event[0] == "config_cas"]
    assert len(updates) == 1 and updates[0][2][3] == "false"
    assert e.journal.exists()


async def test_reply_oracle_rejects_fixed_reply_extras_and_wrong_kind_language():
    step = {"kind": "safety_check", "question": "meaning"}
    payload = {"reply": {"content": replay.QUESTIONS["ko"][1]}, "greeting": None}
    replay.check_reply(payload, step, "ko")
    with pytest.raises(replay.ReplayError, match="question_text_mismatch"):
        replay.check_reply(payload, step, "en")
    payload["reply"]["references"] = [{"synthetic": True}]
    with pytest.raises(replay.ReplayError, match="fixed_reply_has_extras"):
        replay.check_reply(payload, step, "ko")


async def test_cleanup_retains_journal_when_deployed_privacy_worker_does_not_finish(cleanup, monkeypatch):
    e = cleanup
    e.flags["remaining"] = 1
    e.flags["barrier"] = "deleting"
    elapsed = 0
    def clock():
        return elapsed
    async def sleep(seconds):
        nonlocal elapsed
        elapsed += 91
    monkeypatch.setattr(replay.time, "monotonic", clock)
    monkeypatch.setattr(replay.asyncio, "sleep", sleep)
    assert not await e.runner.cleanup(e.state, e.journal)
    assert e.journal.exists() and e.events.count("verify_remaining") == 2
    assert e.emitted[-1]["failure"] == "cleanup_incomplete"


@pytest.mark.parametrize("document", [
    {"items": [{"evidence": [{"message_id": 17, "start_utf8": 0, "end_utf8": 3}]}]},
    {"items": [{"source_message_id": 17, "scope_end": {"source_message_id": 18}}]},
    {"items": [{"scope_start_message_id": 17}], "source_message_ids": [18]},
])
async def test_safety_provenance_check_rejects_json_only_checkpoint_or_contract_source(document):
    calls = []
    uid = uuid.uuid4()
    class DB:
        async def fetch(self, sql, *args):
            assert args == (uid,)
            return [{"document": document}]
        async def fetchval(self, sql, *args):
            calls.append((sql, args))
            if len(args) == 1:
                return 0  # relational mem0/event joins are clean
            assert args[0] == uid and 17 in args[1]
            return 1
    with pytest.raises(replay.ReplayError, match="safety_source_in_contract_or_checkpoint"):
        await runner(DB()).derived_safety_sources(uid)
    assert len(calls) == 2


def test_report_cannot_export_unvalidated_strings():
    with pytest.raises(replay.ReplayError, match="report_invalid"):
        replay.report(1, "ko", "normal", 1, "synthetic private exception with spaces")
    with pytest.raises(replay.ReplayError, match="report_invalid"):
        replay.report(1, "synthetic-private-language", "normal", 1, None)


@pytest.fixture
def parallel_execution(tmp_path, monkeypatch):
    """Exercise execute itself, with a fake DB protocol rejecting overlapping commands."""
    import asyncpg
    import dotenv
    import httpx
    from app.core import db as application_db
    from db import envfile
    from worker import privacy_sweep_jobs
    original_sleep = asyncio.sleep
    connections, events = [], []
    control = SimpleNamespace(fail_connect=None, fail_close=None, connect_calls=0,
                              concurrent=0, maximum=0)
    class Connection:
        def __init__(self, number):
            self.number, self.busy, self.closed = number, False, False
        async def query(self, label):
            assert not self.closed and not self.busy, "another operation is in progress"
            self.busy = True
            if self.number:
                control.concurrent += 1
                control.maximum = max(control.maximum, control.concurrent)
            events.append((self.number, label))
            try:
                await original_sleep(0)
                await original_sleep(0)
            finally:
                self.busy = False
                if self.number:
                    control.concurrent -= 1
        async def close(self):
            assert not self.busy
            self.closed = True
            events.append((self.number, "closed"))
            if self.number == control.fail_close:
                raise RuntimeError("synthetic close failure")
    async def connect(*args, **kwargs):
        number = control.connect_calls
        control.connect_calls += 1
        if number == control.fail_connect:
            raise RuntimeError("synthetic connection failure")
        connection = Connection(number)
        connections.append(connection)
        return connection
    class Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            assert all(connection.closed for connection in connections)
            events.append(("http", "closed"))
    class FakeRunner:
        def __init__(self, db, client, admin, public, expected_sha, emit, **kwargs):
            self.db, self.admin = db, admin
        async def health(self):
            await self.db.query("health")
        async def synthetic_user(self, scenario, saved, journal):
            await self.db.query("create_user")
            uid = uuid.UUID(int=scenario["number"])
            saved["users"].append(str(uid))
            return uid, "synthetic-token"
        async def configure(self, *args):
            await self.db.query("configure")
        async def send(self, *args):
            await self.db.query("send_snapshot")
            await self.db.query("send_verify")
            return False
        async def wait_derived(self, *args):
            await self.db.query("wait_derived")
        async def derived_safety_sources(self, *args):
            await self.db.query("check_sources")
        async def cleanup(self, *args):
            assert all(connection.closed for connection in connections if connection.number)
            assert control.concurrent == 0
            await self.db.query("cleanup")
            return True
    async def fast_sleep(*args):
        await original_sleep(0)
    def configure(*args):
        events.append(("domain", "configure"))
        return env()["SUPABASE_DB_CONNECTION_STRING"]
    async def privacy_step(*args):
        raise AssertionError("mocked main runner must not perform real cleanup")
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *args, **kwargs: env())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr(replay, "Runner", FakeRunner)
    monkeypatch.setattr(replay.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(envfile, "configure_application_db", configure)
    monkeypatch.setattr(application_db, "_engine", None)
    monkeypatch.setattr(application_db, "_sessionmaker", None)
    monkeypatch.setattr(privacy_sweep_jobs, "_sweep_subject", privacy_step)
    monkeypatch.setenv("MOLY_ENV_FILE", "unused-before-test")
    scenarios = [{"number": number, "language": "ko", "steps": [{}]} for number in range(1, 4)]
    path = private(tmp_path, data=corpus())
    args = SimpleNamespace(env_file="unused", expected_sha="a"*40,
                           input=str(path), cleanup_state=None)
    return SimpleNamespace(args=args, scenarios=scenarios, checkout=tmp_path / "checkout",
        control=control, connections=connections, events=events)


async def test_parallel_scenarios_use_distinct_connections_and_control_connection_only_after_join(parallel_execution):
    e = parallel_execution
    assert await replay.execute(e.args, e.scenarios, checkout=e.checkout)
    assert len(e.connections) == 4 and all(connection.closed for connection in e.connections)
    assert e.control.maximum == 2
    cleanup_index = e.events.index((0, "cleanup"))
    assert all(e.events.index((number, "closed")) < cleanup_index for number in range(1, 4))


@pytest.mark.parametrize("failure", ["fail_connect", "fail_close"])
async def test_parallel_connection_failure_waits_for_siblings_before_cleanup(parallel_execution, failure):
    e = parallel_execution
    setattr(e.control, failure, 2)
    assert not await replay.execute(e.args, e.scenarios, checkout=e.checkout)
    assert e.control.connect_calls == 4 and all(connection.closed for connection in e.connections)
    cleanup_index = e.events.index((0, "cleanup"))
    assert all(e.events.index((connection.number, "closed")) < cleanup_index
               for connection in e.connections if connection.number)


@pytest.mark.parametrize(("payload", "expected"), [
    ({"error": {"code": "AI_UNAVAILABLE", "message": "synthetic private body"}}, "http_503_ai_unavailable"),
    ({"error": {"code": "synthetic_private_code"}}, "http_503"),
    ({"error": {"code": {"synthetic": "private"}}}, "http_503"),
    ({"error": {"code": ["synthetic"]}}, "http_503"),
])
def test_response_failure_only_emits_known_enums(payload, expected):
    assert replay.response_failure(SimpleNamespace(status_code=503, json=lambda: payload)) == expected


async def test_expedite_ingest_requires_run_tag_and_only_updates_owned_ready_job_schedule():
    uid, other = uuid.uuid4(), uuid.uuid4()
    jobs = [
        {"uid": uid, "tag": "owned-run", "state": "ready", "type": "mem0_ingest", "at": 180},
        {"uid": uid, "tag": "owned-run", "state": "running", "type": "mem0_ingest", "at": 180},
        {"uid": uid, "tag": "owned-run", "state": "ready", "type": "contract_compile", "at": 180},
        {"uid": other, "tag": "other-run", "state": "ready", "type": "mem0_ingest", "at": 180},
        {"uid": uid, "tag": "owned-run", "state": "ready", "type": "mem0_ingest", "at": 180},
    ]
    for row in jobs:
        row.update(payload={"synthetic": "unchanged"}, attempt=2)
    jobs[0]["attempt"] = 0
    class DB:
        async def execute(self, sql, requested, run_tag):
            assert requested == uid and run_tag == "owned-run"
            assert "SET available_at=LEAST(available_at,now())" in sql
            assert "j.user_id=$1" in sql and "j.job_type='mem0_ingest' AND j.state='ready'" in sql
            assert "j.attempt=0" in sql
            assert "guard_context_replay'=$2" in sql
            for row in jobs:
                if (row["uid"] == requested and row["tag"] == run_tag
                        and row["state"] == "ready" and row["type"] == "mem0_ingest"
                        and row["attempt"] == 0):
                    row["at"] = 0
    active = runner(DB())
    with pytest.raises(replay.ReplayError, match="test_ownership_required"):
        await active.expedite_owned_ingest(uid)
    assert all(row["at"] == 180 for row in jobs)
    active.run_tag = "owned-run"
    await active.expedite_owned_ingest(uid)
    assert [row["at"] for row in jobs] == [0, 180, 180, 180, 180]
    assert [row["attempt"] for row in jobs] == [0, 2, 2, 2, 2]
    assert all(row["payload"] == {"synthetic": "unchanged"} for row in jobs)


async def test_domain_target_must_match_validated_connection_before_any_replay_connection(parallel_execution, monkeypatch):
    from db import envfile
    e = parallel_execution
    def wrong_target(*args):
        return "postgresql://postgres:synthetic@wrong.example:5432/postgres"
    monkeypatch.setattr(envfile, "configure_application_db", wrong_target)
    with pytest.raises(replay.ReplayError):
        await replay.execute(e.args, e.scenarios, checkout=e.checkout)
    assert not e.connections


async def test_non_development_target_is_rejected_before_domain_configuration(parallel_execution, monkeypatch):
    import dotenv
    from db import envfile
    e = parallel_execution
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *args, **kwargs:
                        env() | {"SUPABASE_URL": "https://wrong.example"})
    def forbidden(*args):
        raise AssertionError("domain configuration must follow target validation")
    monkeypatch.setattr(envfile, "configure_application_db", forbidden)
    with pytest.raises(replay.ReplayError, match="development_target_required"):
        await replay.execute(e.args, e.scenarios, checkout=e.checkout)
    assert not e.connections


@pytest.mark.parametrize("eligible", [True, False])
async def test_cleanup_domain_step_only_advances_exact_deleted_journal_subject(cleanup, monkeypatch, eligible):
    e = cleanup
    e.flags["barrier"] = "deleting"
    e.flags["remaining"] = 0 if eligible else 1
    original_fetchrow = e.runner.db.fetchrow
    steps, elapsed = [], 0
    async def fetchrow(sql, *args):
        if "FROM privacy_subject_barriers b" in sql:
            assert args == (e.uid,)
            assert "b.state='deleting'" in sql
            assert "NOT EXISTS(SELECT 1 FROM auth.users" in sql
            assert "NOT EXISTS(SELECT 1 FROM profiles" in sql
            if not eligible:
                return None
            operation = uuid.UUID(e.state["cleanup_operations"][str(e.uid)])
            return {"operation_id": operation, "empty_sweeps": len(steps)}
        return await original_fetchrow(sql, *args)
    async def privacy_step(uid, operation, empty_sweeps):
        assert uid == e.uid and operation == uuid.UUID(e.state["cleanup_operations"][str(e.uid)])
        assert empty_sweeps == len(steps)
        steps.append((uid, operation, empty_sweeps))
        if len(steps) == 2:
            e.flags["barrier"] = "deleted"
        return 0, "empty" if len(steps) == 1 else "closed"
    def clock():
        return elapsed
    async def sleep(seconds):
        nonlocal elapsed
        elapsed += 1 if eligible else 91
    monkeypatch.setattr(e.runner.db, "fetchrow", fetchrow)
    monkeypatch.setattr(replay.time, "monotonic", clock)
    monkeypatch.setattr(replay.asyncio, "sleep", sleep)
    e.runner.privacy_step = privacy_step
    result = await e.runner.cleanup(e.state, e.journal)
    assert result is eligible
    assert len(steps) == (2 if eligible else 0)
    assert e.journal.exists() is not eligible
    if eligible:
        # Two step calls alone are insufficient: require two later empty/deleted reads.
        assert e.events.count("verify_remaining") == 4


async def test_wait_stage_expedites_ingest_successor_created_after_initial_running_job(monkeypatch):
    uid = uuid.uuid4()
    status = {"job": "running", "available_at": 180, "updates": 0}
    class DB:
        async def execute(self, sql, *args):
            assert args == (uid, "owned-run")
            status["updates"] += 1
            if status["job"] == "ready":
                status["available_at"] = 0
        async def fetchval(self, sql, *args):
            if status["job"] == "running":
                # Existing work finishes after the first expedite sees no ready row.
                status["job"] = "ready"
                return False
            return status["available_at"] == 0
    async def sleep(seconds):
        pass
    monkeypatch.setattr(replay.asyncio, "sleep", sleep)
    active = runner(DB())
    active.run_tag, active.recovery_timeout = "owned-run", 150
    await active.wait_processed(uid, 2)
    assert status["updates"] >= 2 and status["available_at"] == 0


@pytest.mark.parametrize("scheduled_contract", [False, True])
async def test_derived_wait_does_not_require_unscheduled_contract_but_drains_existing_jobs(monkeypatch, scheduled_contract):
    uid = uuid.uuid4()
    queries, updates, pending_checks = [], [], 0
    class DB:
        async def execute(self, sql, *args):
            assert "UPDATE async_jobs" in sql and "j.attempt=0" in sql
            updates.append(sql)
        async def fetchval(self, sql, *args):
            nonlocal pending_checks
            queries.append(sql)
            if "SELECT max(id) FROM messages" in sql:
                return 8
            if "p.consolidated_through_turn_seq" in sql:
                # All facts are settled; a small same-day chat's contract cursor
                # is intentionally still zero and has no contract job.
                assert "contract_processed_through_message_id" not in sql
                assert "contract_generation" not in sql
                assert "c.status='planned'" in sql and "r.semantic_status='pending'" in sql
                assert "r.canonical_text IS NULL" in sql and "m.turn_seq IS NULL" in sql
                return True
            assert "state IN ('ready','running')" in sql
            pending_checks += 1
            return scheduled_contract and pending_checks == 1
    async def sleep(seconds):
        pass
    monkeypatch.setattr(replay.asyncio, "sleep", sleep)
    active = runner(DB())
    active.run_tag, active.recovery_timeout = "owned-run", 150
    await active.wait_derived(uid)
    assert pending_checks == (2 if scheduled_contract else 1)
    assert updates and all("INSERT" not in query for query in queries+updates)


async def test_unsettled_fact_processing_still_times_out(monkeypatch):
    elapsed = 0
    class DB:
        async def fetchval(self, sql, *args):
            assert "p.consolidated_through_turn_seq" in sql
            return False
    def clock():
        return elapsed
    async def sleep(seconds):
        nonlocal elapsed
        elapsed += 1
    monkeypatch.setattr(replay.time, "monotonic", clock)
    monkeypatch.setattr(replay.asyncio, "sleep", sleep)
    active = runner(DB())
    active.recovery_timeout = 2
    with pytest.raises(replay.ReplayError, match="fact_processing_timeout"):
        await active.wait_processed(uuid.uuid4(), 8)


async def test_actual_same_day_boundary_does_not_schedule_contract_work(monkeypatch):
    from app.services import jobs, memory_pipeline
    scheduled = []
    class Session:
        async def execute(self, sql, params):
            assert sql is memory_pipeline._DAY_BOUNDARY
            return SimpleNamespace(first=lambda: (date(2026, 11, 3), date(2026, 11, 3)))
    async def enqueue(*args, **kwargs):
        scheduled.append(kwargs)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    assert await memory_pipeline.enqueue_day_boundary_jobs(Session(), uuid.uuid4(), turn_seq=8) is None
    assert not scheduled


@pytest.mark.parametrize(("kind", "pressure", "expected"), [
    ("normal", 2, True), ("normal", None, False), ("safety", None, False),
])
async def test_send_recognizes_proactive_pressure_without_requiring_a_503(kind, pressure, expected):
    uid = uuid.uuid4()
    sent = False
    queries = []
    class DB:
        async def fetchrow(self, sql, *args):
            return {"messages": 2 if sent else 0, "tokens": 0}
        async def fetch(self, sql, *args):
            queries.append(sql)
            assert args == (uid, [1, 2])
            if "content_hash" in sql:
                return [{"id": 1, "sender": "user", "kind": kind, "content_hash": "a"*32},
                        {"id": 2, "sender": "moly", "kind": kind, "content_hash": "b"*32}]
            return [{"sender": "user", "kind": kind}, {"sender": "moly", "kind": kind}]
        async def fetchval(self, sql, *args):
            queries.append(sql)
            assert args == (uid,)
            assert "j.job_type='conversation_checkpoint'" in sql
            assert "j.payload->>'reason'='input_pressure'" in sql
            assert "j.payload->'memory_generation'=to_jsonb(cc.memory_generation)" in sql
            return pressure
    class Client:
        async def post(self, url, **kwargs):
            nonlocal sent
            assert url == replay.API + "/chat/messages"
            sent = True
            content = replay.GUIDES["ko"] if kind == "safety" else "합성 답변"
            return SimpleNamespace(status_code=200, json=lambda: {
                "user_message": {"message_id": "1"}, "reply": {"message_id": "2", "content": content},
                "greeting": None})
    active = runner(DB(), Client())
    scenario = {"number": 1, "language": "ko", "require_recovery": expected}
    assert await active.send(uid, "synthetic-token", scenario, {"text": "합성", "kind": kind}) is expected
    assert active.pressure_boundaries.get(uid) == pressure
    assert bool(active.raw_proofs.get(uid)) is expected
    assert all("UPDATE" not in query and "INSERT" not in query for query in queries)


@pytest.mark.parametrize(("failure", "reason"), [
    (None, None), ("missing_checkpoint", "recovery_checkpoint_not_published"),
    ("unvalidated", "recovery_checkpoint_not_published"),
    ("partial_coverage", "recovery_checkpoint_coverage_missing"),
    ("incomplete_processing", "recovery_source_processing_incomplete"),
    ("changed_source", "recovery_raw_messages_changed"),
    ("deleted_source", "recovery_raw_messages_missing"),
])
async def test_required_recovery_proves_publication_full_processing_and_preserved_raw_sources(failure, reason):
    uid = uuid.uuid4()
    queries = []
    class DB:
        async def fetchrow(self, sql, *args):
            queries.append(sql)
            assert args == (uid,)
            assert "c.memory_generation=cc.memory_generation" in sql and "c.publish_state='published'" in sql
            if failure == "missing_checkpoint":
                return None
            return {"generation": 7, "through_message_id": 1 if failure == "partial_coverage" else 2,
                    "processing_complete": failure != "unvalidated"}
        async def fetchval(self, sql, *args):
            queries.append(sql)
            assert args == (uid, 7, 2)
            assert "id<=$3 AND kind='normal'" in sql
            assert "cc.memory_generation=$2" in sql and "cc.contract_generation=$2" in sql
            assert "cc.contract_processed_through_message_id>=n.last_id" in sql
            assert "p.consolidated_through_turn_seq>=n.last_turn" in sql
            assert "c.status='planned'" in sql and "r.canonical_text IS NULL" in sql
            return failure != "incomplete_processing"
        async def fetch(self, sql, *args):
            queries.append(sql)
            assert args == (uid, [1, 2])
            rows = [{"id": 1, "sender": "user", "kind": "normal", "content_hash": "a"*32},
                    {"id": 2, "sender": "moly", "kind": "normal", "content_hash": "b"*32}]
            if failure == "changed_source":
                rows[0]["content_hash"] = "c"*32
            return rows[:1] if failure == "deleted_source" else rows
    active = runner(DB())
    active.pressure_boundaries[uid] = 2
    active.raw_proofs[uid] = {1: ("user", "normal", "a"*32), 2: ("moly", "normal", "b"*32)}
    if reason:
        with pytest.raises(replay.ReplayError, match=reason):
            await active.verify_recovery(uid)
    else:
        await active.verify_recovery(uid)
    assert all("UPDATE " not in query and "INSERT " not in query and "DELETE " not in query for query in queries)
