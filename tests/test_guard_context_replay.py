"""개발 리플레이 도구의 경계·설정 복구·privacy 정리. 네트워크/DB 호출 없음."""
from datetime import datetime, timezone
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
