"""Opt-in replay against the existing development API and Supabase only.

No application module is imported: importing this module neither reads .env nor
opens a provider/DB client. Input is a private, pre-redacted JSON file outside
the checkout. Responses, input, credentials and identifiers are never printed.

Input v1: {"schema":"guard-context-replay-v1","redaction_reviewed":true,
 "scenarios":[{"number":1,"language":"ko","origin":"synthetic",
 "steps":[{"text":"...","kind":"normal","replay":true}]}]}.
origin may instead be "deidentified"; this provenance is retained in the private
input, never relabelled as synthetic. Optional step fields: question
(thought/meaning/method_thought/method_meaning), contains, absent,
wait_processing, allow_recovery. Optional scenario: require_recovery.

Run only after the proposed code and reviewed schema are on development:
  python scripts/replay_guard_context_dev.py --env-file /private/.../dev.env \
    --input /private/.../corpus.json --expected-sha <40 hex> --execute-dev

Default invocation is validation only. A crash leaves a private cleanup journal
beside the input; --cleanup-state <journal> --execute-dev resumes cleanup. The
journal holds only synthetic account IDs/configuration state, never chat text or
tokens. Existing cohorts must already be enabled with this policy, or be empty;
this tool will not enable an existing unrelated cohort.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
from typing import Any, Callable
from urllib.parse import unquote, urlsplit
import uuid

API = "https://dev.moly.asia"
REF = "wywzjslvxwttxkecbyis"
AUTH = f"https://{REF}.supabase.co"
POLICY = "crisis-v2"
KINDS = {"normal", "safety_check", "safety"}
CONFIG_KEYS = tuple(sorted(("crisis_guard_enabled", "crisis_guard_policy_version",
    "crisis_guard_timeout_s", "crisis_guard_history_messages", "crisis_guard_ready_users",
    "context_retention_v2_enabled", "context_retention_ready_users")))
QUESTIONS = {
    "ko": ("무슨 일 있었어?", "어떤 뜻인지 조금만 더 얘기해줄래?", "그건 알려 줄 수 없어."),
    "en": ("What happened?", "Can you tell me a little more about what you mean?",
           "I can't tell you that."),
    "ja": ("何があったの？", "どういう意味か、もう少し教えてくれる？", "それは教えられないよ。"),
}
# Independent oracle: do not render expected text through the implementation.
GUIDES = {
    "ko": "이 서비스는 의료 서비스가 아니며, 진단이나 치료를 제공하지 않습니다. "
          "상담이 필요하면 https://findahelpline.com/ 에서 현재 계신 나라의 상담기관을 찾아 연락하세요.",
    "en": "This is not a medical service and does not provide diagnosis or treatment. "
          "For crisis support, select the country you are in at https://findahelpline.com/ "
          "and contact a local helpline.",
    "ja": "本サービスは医療サービスではなく、診断や治療は行いません。"
          "相談が必要な場合は、https://findahelpline.com/ で現在いる国の相談窓口を探し、連絡してください。",
}


class ReplayError(Exception):
    """Only fixed internal reason codes may cross the reporting boundary."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ReplayError(reason)


def report(number: int, language: str, route: str, latency: int, failure: str | None) -> dict:
    # All arguments except numeric latency are validated independently of source
    # labels, HTTP error bodies, SQL exceptions and generated model output.
    require(type(number) is int and number >= 0, "report_invalid")
    require(language in {"ko", "en", "ja", "none"}, "report_invalid")
    require(route in KINDS | {"setup", "cleanup", "validation", "unknown"}, "report_invalid")
    require(failure is None or re.fullmatch(r"[a-z][a-z0-9_]{0,79}", failure) is not None,
            "report_invalid")
    return {"scenario": number, "language": language, "route": route,
            "latency_ms": max(0, int(latency)), "failure": failure}


def private_file(path: Path, *, checkout: Path) -> Path:
    require(path.is_absolute() and not path.is_symlink(), "private_file_required")
    resolved = path.resolve()
    require(not resolved.is_relative_to(checkout.resolve()), "private_file_in_checkout")
    require(not any((parent / ".git").exists() for parent in resolved.parents),
            "private_file_in_checkout")
    require(resolved.is_file(), "private_file_required")
    require(resolved.stat().st_uid == os.getuid() and
            stat.S_IMODE(resolved.stat().st_mode) & 0o077 == 0, "private_file_permissions")
    require(resolved.parent.stat().st_uid == os.getuid() and
            stat.S_IMODE(resolved.parent.stat().st_mode) & 0o077 == 0,
            "private_directory_permissions")
    return resolved


def load_corpus(path: Path, *, checkout: Path) -> list[dict]:
    path = private_file(path, checkout=checkout)
    require(path.stat().st_size <= 2_000_000, "corpus_too_large")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ReplayError("corpus_invalid") from None
    require(isinstance(data, dict) and data.get("schema") == "guard-context-replay-v1"
            and data.get("redaction_reviewed") is True, "redaction_review_required")
    scenarios = data.get("scenarios")
    require(isinstance(scenarios, list) and 0 < len(scenarios) <= 100, "corpus_invalid")
    seen: set[int] = set()
    for scenario in scenarios:
        require(isinstance(scenario, dict), "corpus_invalid")
        number = scenario.get("number")
        require(type(number) is int and number > 0 and number not in seen, "scenario_number_invalid")
        seen.add(number)
        require(scenario.get("language") in QUESTIONS and
                scenario.get("origin") in {"synthetic", "deidentified"}, "corpus_invalid")
        steps = scenario.get("steps")
        require(isinstance(steps, list) and 0 < len(steps) <= 100, "corpus_invalid")
        require(type(scenario.get("require_recovery", False)) is bool, "corpus_invalid")
        for step in steps:
            require(isinstance(step, dict), "corpus_invalid")
            text = step.get("text")
            require(isinstance(text, str) and 1 <= len(text) <= 2000, "step_text_invalid")
            require(step.get("kind") in KINDS, "step_kind_invalid")
            question = step.get("question")
            require(question in {None, "thought", "meaning", "method_thought", "method_meaning"},
                    "step_question_invalid")
            require(step["kind"] == "safety_check" or question is None, "step_question_invalid")
            for key in ("replay", "wait_processing", "allow_recovery"):
                require(type(step.get(key, False)) is bool, "corpus_invalid")
            for key in ("contains", "absent"):
                require(isinstance(step.get(key, []), list) and
                        all(isinstance(v, str) and 0 < len(v) <= 300 for v in step.get(key, [])),
                        "step_assertion_invalid")
            # This is a second barrier, not a claim to automatic deidentification.
            # Real names must already have been reviewed/redacted by the supplier.
            private_values = [text, *step.get("contains", []), *step.get("absent", [])]
            for value in private_values:
                require(not re.search(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|"
                                      r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b|"
                                      r"(?:Bearer\s|sk-[A-Za-z0-9_-]{12})|"
                                      r"https?://[^\s]+[?&](?:token|key|secret)=", value),
                        "unredacted_identifier_detected")
    return scenarios


def validate_targets(env: dict, expected_sha: str) -> str:
    require(re.fullmatch(r"[0-9a-f]{40}", expected_sha) is not None, "expected_sha_required")
    require((env.get("SUPABASE_URL") or "").rstrip("/") == AUTH, "development_target_required")
    dsn = re.sub(r"^postgresql\+asyncpg://", "postgresql://",
                 env.get("SUPABASE_DB_CONNECTION_STRING") or "")
    try:
        target = urlsplit(dsn)
        host = target.hostname or ""
        valid_host = host == f"db.{REF}.supabase.co" or bool(
            re.fullmatch(r"aws-[0-9]+-[a-z0-9-]+\.pooler\.supabase\.com", host))
        require(target.scheme in {"postgres", "postgresql"} and valid_host and
                unquote(target.username or "") == f"postgres.{REF}" and
                target.port in {5432, 6543} and target.path == "/postgres" and
                not target.query and not target.fragment, "development_target_required")
    except ValueError:
        raise ReplayError("development_target_required") from None
    require(bool(env.get("SUPABASE_SECRET_KEY") or env.get("SUPABASE_SERVICE_ROLE_KEY")) and
            bool(env.get("SUPABASE_PUBLISHABLE_KEY") or env.get("SUPABASE_ANON_KEY")),
            "auth_configuration_required")
    return dsn


def config_patch(saved: dict, users: list[str]) -> dict:
    """Never enable or switch the policy underneath unrelated existing cohorts."""
    for flag, cohort in (("crisis_guard_enabled", "crisis_guard_ready_users"),
                         ("context_retention_v2_enabled", "context_retention_ready_users")):
        values = saved.get(cohort, [])
        require(isinstance(values, list) and all(isinstance(u, str) for u in values),
                "existing_cohort_invalid")
        try:
            require(all(str(uuid.UUID(u)) == u for u in values), "existing_cohort_invalid")
        except ValueError:
            raise ReplayError("existing_cohort_invalid") from None
        require(not values or saved.get(flag) is True, "existing_cohort_would_be_enabled")
        if cohort == "crisis_guard_ready_users":
            require(not values or saved.get("crisis_guard_policy_version") == POLICY,
                    "existing_cohort_policy_conflict")
            require(not values or (saved.get("crisis_guard_timeout_s", 2.0) == 2.0 and
                    saved.get("crisis_guard_history_messages", 6) == 6),
                    "existing_cohort_settings_conflict")
    return {"crisis_guard_enabled": True, "crisis_guard_policy_version": POLICY,
            "crisis_guard_timeout_s": 2.0, "crisis_guard_history_messages": 6,
            "crisis_guard_ready_users": saved.get("crisis_guard_ready_users", []) + users,
            "context_retention_v2_enabled": True,
            "context_retention_ready_users": saved.get("context_retention_ready_users", []) + users}


def expected_question(language: str, question: str | None) -> set[str]:
    thought, meaning, refusal = QUESTIONS[language]
    if question is None:
        return {thought, meaning, f"{refusal} {thought}", f"{refusal} {meaning}"}
    bare = meaning if question.endswith("meaning") else thought
    return {f"{refusal} {bare}" if question.startswith("method_") else bare}


def check_reply(payload: dict, step: dict, language: str) -> None:
    try:
        reply = payload["reply"]["content"]
        require(isinstance(reply, str), "response_invalid")
        if step["kind"] != "normal":
            require(payload.get("greeting") is None and not payload["reply"].get("references"),
                    "fixed_reply_has_extras")
        if step["kind"] == "safety":
            require(reply == GUIDES[language], "guide_text_mismatch")
        elif step["kind"] == "safety_check":
            require(reply in expected_question(language, step.get("question")), "question_text_mismatch")
        require(all(v.casefold() in reply.casefold() for v in step.get("contains", [])),
                "expected_fact_missing")
        require(all(v.casefold() not in reply.casefold() for v in step.get("absent", [])),
                "excluded_content_returned")
    except (KeyError, TypeError):
        raise ReplayError("response_invalid") from None


def decode_json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def save_journal(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".pending")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(state, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class Runner:
    db: Any
    client: Any
    admin: dict
    public: str
    expected_sha: str
    emit: Callable[[dict], None]
    recovery_timeout: float = 150.0

    async def health(self) -> None:
        response = await self.client.get(f"{API}/health")
        require(response.status_code == 200, "development_health_failed")
        data = response.json()
        require(data.get("env") == "development" and data.get("version") == self.expected_sha,
                "development_version_mismatch")

    async def synthetic_user(self, scenario: dict, state: dict, journal: Path) -> tuple[uuid.UUID, str]:
        email = f"guard-v2-{uuid.uuid4().hex}@moly.test"
        response = await self.client.post(f"{AUTH}/auth/v1/admin/users", headers=self.admin,
            json={"email": email, "password": secrets.token_urlsafe(32), "email_confirm": True,
                  "user_metadata": {"guard_context_replay": state["run"]}})
        require(response.status_code in {200, 201}, "synthetic_user_creation_failed")
        uid = uuid.UUID(response.json()["id"])
        state["users"].append(str(uid))
        save_journal(journal, state)
        # Fresh synthetic accounts have no history to bootstrap or contamination
        # to adopt. Real-source text is replayed only after deidentification.
        async with self.db.transaction():
            await self.db.execute("""UPDATE profiles SET nickname='테스터',language=$2,
              timezone='Asia/Seoul',app_trial_started_at=now(),
              app_trial_ends_at=now()+interval '1 day' WHERE id=$1""", uid, scenario["language"])
            await self.db.execute("""INSERT INTO chat_contexts(user_id,memory_mode)
              VALUES($1,'normalized') ON CONFLICT(user_id) DO UPDATE SET memory_mode='normalized'""", uid)
            await self.db.execute("""INSERT INTO memory_pipeline_states
              (user_id,mode,bootstrap_status,historical_upper_turn_seq)
              VALUES($1,'v2','ready',0) ON CONFLICT(user_id) DO UPDATE
              SET mode='v2',bootstrap_status='ready',historical_upper_turn_seq=0""", uid)
        response = await self.client.post(f"{AUTH}/auth/v1/admin/generate_link", headers=self.admin,
                                         json={"type": "magiclink", "email": email})
        require(response.status_code == 200, "synthetic_authentication_failed")
        response = await self.client.post(f"{AUTH}/auth/v1/verify", headers={"apikey": self.public},
            json={"type": "magiclink", "token_hash": response.json()["hashed_token"]})
        require(response.status_code == 200, "synthetic_authentication_failed")
        return uid, response.json()["access_token"]

    async def configure(self, state: dict, journal: Path) -> None:
        async with self.db.transaction():
            rows = await self.db.fetch("""SELECT key,value,updated_at FROM app_config
              WHERE key=ANY($1::text[]) ORDER BY key FOR UPDATE""", list(CONFIG_KEYS))
            saved = {row["key"]: decode_json(row["value"]) for row in rows}
            patch = config_patch(saved, state["users"])
            if saved.get("context_retention_v2_enabled") is not True:
                adopted = await self.db.fetchval("""SELECT EXISTS(
                  SELECT 1 FROM conversation_checkpoints c JOIN chat_contexts cc ON cc.user_id=c.user_id
                  WHERE c.memory_generation=cc.memory_generation
                    AND c.version='conversation-checkpoint-summary-v5' AND c.publish_state='published'
                    AND NOT(c.user_id=ANY($1::uuid[])))""", [uuid.UUID(u) for u in state["users"]])
                require(not adopted, "existing_adoption_would_be_enabled")
            state["saved"] = {row["key"]: {"value": decode_json(row["value"]),
                               "updated_at": row["updated_at"].isoformat()} for row in rows}
            # Persist intent BEFORE the commit so a process crash never leaves an
            # enabled flag with no restoration evidence. Written timestamps use
            # one explicit transaction timestamp, known before any mutations.
            stamp = await self.db.fetchval("SELECT transaction_timestamp()")
            state["written"] = {key: {"value": value, "updated_at": stamp.isoformat()}
                                for key, value in patch.items()}
            save_journal(journal, state)
            for key, value in patch.items():
                if key in state["saved"]:
                    changed = await self.db.fetchval("""UPDATE app_config SET value=$2::jsonb,updated_at=$3
                      WHERE key=$1 AND value=$4::jsonb AND updated_at=$5 RETURNING key""", key,
                      json.dumps(value), stamp, json.dumps(saved[key]),
                      datetime.fromisoformat(state["saved"][key]["updated_at"]))
                else:
                    changed = await self.db.fetchval("""INSERT INTO app_config(key,value,updated_at)
                      VALUES($1,$2::jsonb,$3) ON CONFLICT DO NOTHING RETURNING key""",
                      key, json.dumps(value), stamp)
                require(changed == key, "configuration_changed_concurrently")

    async def snapshot(self, uid: uuid.UUID) -> tuple[int, int]:
        row = await self.db.fetchrow("""SELECT
          (SELECT count(*) FROM messages WHERE user_id=$1) AS messages,
          (SELECT COALESCE(sum(tokens_used),0) FROM user_daily_stats WHERE user_id=$1) AS tokens""", uid)
        return int(row["messages"]), int(row["tokens"])

    async def wait_processed(self, uid: uuid.UUID, through: int) -> None:
        end = time.monotonic() + self.recovery_timeout
        while time.monotonic() < end:
            complete = await self.db.fetchval("""SELECT
              cc.contract_generation=cc.memory_generation AND
              cc.contract_processed_through_message_id >= COALESCE((SELECT max(id) FROM messages
                WHERE user_id=$1 AND kind='normal' AND id<=$2),0) AND
              p.mode='v2' AND p.bootstrap_status='ready' AND
              NOT EXISTS(SELECT 1 FROM messages m WHERE m.user_id=$1 AND m.kind='normal'
                AND m.id <= $2 AND (m.turn_seq > p.consolidated_through_turn_seq
                  OR (m.sender='user' AND m.turn_seq IS NULL))) AND
              NOT EXISTS(SELECT 1 FROM mem0_ingest_candidates c WHERE c.user_id=$1 AND c.status='planned'
                AND c.turn_seq <= COALESCE((SELECT max(turn_seq) FROM messages
                  WHERE user_id=$1 AND kind='normal' AND id<=$2),0)) AND
              NOT EXISTS(SELECT 1 FROM mem0_memory_registry r WHERE r.user_id=$1
                AND r.source_turn_seq <= COALESCE((SELECT max(turn_seq) FROM messages
                  WHERE user_id=$1 AND kind='normal' AND id<=$2),0)
                AND (r.semantic_status='pending' OR
                  (r.semantic_status='active' AND r.canonical_text IS NULL)))
              FROM chat_contexts cc JOIN memory_pipeline_states p ON p.user_id=cc.user_id
              WHERE cc.user_id=$1""", uid, through)
            if complete is True:
                return
            await asyncio.sleep(0.5)
        raise ReplayError("fact_contract_processing_timeout")

    async def wait_derived(self, uid: uuid.UUID) -> None:
        through = await self.db.fetchval("SELECT max(id) FROM messages WHERE user_id=$1 AND kind='normal'", uid)
        if through is not None:
            await self.wait_processed(uid, through)
        deadline = time.monotonic() + self.recovery_timeout
        while time.monotonic() < deadline:
            pending = await self.db.fetchval("""SELECT EXISTS(SELECT 1 FROM async_jobs
              WHERE user_id=$1 AND state IN ('ready','running'))""", uid)
            if not pending:
                return
            await asyncio.sleep(0.5)
        raise ReplayError("derived_processing_timeout")

    async def send(self, uid: uuid.UUID, token: str, scenario: dict, step: dict) -> bool:
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": f"guard-v2-{uuid.uuid4().hex}"}
        body = {"text": step["text"], "locale": scenario["language"]}
        before = await self.snapshot(uid)
        started = time.monotonic()
        recovered = False
        end = started + self.recovery_timeout
        while True:
            response = await self.client.post(f"{API}/chat/messages", headers=headers, json=body)
            if response.status_code == 200:
                break
            # A pressure pause is not a completed turn. Retry exactly the same
            # key, never duplicate the current user message to force progress.
            require(step.get("allow_recovery", False) and response.status_code == 503,
                    f"http_{response.status_code}")
            require(await self.snapshot(uid) == before, "paused_request_committed_side_effects")
            pressure = await self.db.fetchval("""SELECT retention_pressure_through_message_id
              FROM chat_contexts WHERE user_id=$1""", uid)
            require(pressure is not None or recovered, "pressure_target_missing")
            recovered = True
            require(time.monotonic() < end, "pressure_recovery_timeout")
            await asyncio.sleep(0.5)
        payload = response.json()
        check_reply(payload, step, scenario["language"])
        ids = [int(payload["user_message"]["message_id"]), int(payload["reply"]["message_id"])]
        rows = await self.db.fetch("""SELECT sender,kind FROM messages
          WHERE user_id=$1 AND id=ANY($2::bigint[])""", uid, ids)
        require(len(rows) == 2 and {r["sender"] for r in rows} == {"user", "moly"}
                and all(r["kind"] == step["kind"] for r in rows), "message_kinds_mismatch")
        after = await self.snapshot(uid)
        require(after[0] == before[0] + 2, "turn_message_count_mismatch")
        if step["kind"] != "normal":
            require(after[1] == before[1], "fixed_reply_charged")
        if step.get("replay"):
            replay = await self.client.post(f"{API}/chat/messages", headers=headers, json=body)
            require(replay.status_code == 200 and replay.json() == payload, "idempotency_result_changed")
            require(await self.snapshot(uid) == after, "idempotency_side_effects_duplicated")
        if step.get("wait_processing"):
            await self.wait_processed(uid, max(ids))
        self.emit(report(scenario["number"], scenario["language"], step["kind"],
                         round((time.monotonic() - started) * 1000), None))
        return recovered

    async def derived_safety_sources(self, uid: uuid.UUID) -> None:
        # Inspect provenance joins, not literal substring matches in summaries:
        # absence of a word is not proof that a source was excluded.
        bad = await self.db.fetchval("""SELECT
          (SELECT count(*) FROM mem0_ingest_candidate_sources s JOIN messages m
            ON m.user_id=s.user_id AND m.id=s.source_message_id
            WHERE s.user_id=$1 AND m.kind IN ('safety','safety_check')) +
          (SELECT count(*) FROM mem0_memory_sources s JOIN messages m
            ON m.user_id=s.user_id AND m.id=s.source_message_id
            WHERE s.user_id=$1 AND m.kind IN ('safety','safety_check')) +
          (SELECT count(*) FROM relationship_events e JOIN messages m
            ON m.user_id=e.user_id AND m.turn_seq=e.turn_seq
            WHERE e.user_id=$1 AND m.kind IN ('safety','safety_check'))""", uid)
        require(bad == 0, "safety_source_in_derivatives")
        documents = await self.db.fetch("""SELECT state_json AS document FROM conversation_checkpoints
          WHERE user_id=$1 AND state_json IS NOT NULL
          UNION ALL SELECT document_json FROM user_interaction_contracts WHERE user_id=$1""", uid)
        refs: set[int] = set()

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"message_id", "source_message_id", "scope_start_message_id"}:
                        if type(item) is int:
                            refs.add(item)
                    elif key == "source_message_ids" and isinstance(item, list):
                        refs.update(v for v in item if type(v) is int)
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        for row in documents:
            collect(decode_json(row["document"]))
        if refs:
            bad = await self.db.fetchval("""SELECT count(*) FROM messages
              WHERE user_id=$1 AND id=ANY($2::bigint[]) AND kind IN ('safety','safety_check')""",
              uid, sorted(refs))
            require(bad == 0, "safety_source_in_contract_or_checkpoint")

    async def cleanup(self, state: dict, journal: Path) -> bool:
        errors = False
        # Recover Auth creations whose successful response was lost before the
        # ID reached this process. The random run tag is exact and private.
        try:
            owned = await self.db.fetch("""SELECT id FROM auth.users
              WHERE raw_user_meta_data->>'guard_context_replay'=$1""", state["run"])
            state["users"] = sorted(set(state.get("users", [])) | {str(row["id"]) for row in owned})
            save_journal(journal, state)
        except Exception:
            errors = True
        # Restore only values and timestamps this exact run wrote. A concurrent
        # administrator edit always wins; do not silently overwrite it.
        for key, written in state.get("written", {}).items():
            try:
                saved = state.get("saved", {}).get(key)
                if saved is None:
                    result = await self.db.execute("""DELETE FROM app_config
                      WHERE key=$1 AND value=$2::jsonb AND updated_at=$3""", key,
                      json.dumps(written["value"]), datetime.fromisoformat(written["updated_at"]))
                else:
                    result = await self.db.execute("""UPDATE app_config SET value=$4::jsonb,updated_at=now()
                      WHERE key=$1 AND value=$2::jsonb AND updated_at=$3""", key,
                      json.dumps(written["value"]), datetime.fromisoformat(written["updated_at"]),
                      json.dumps(saved["value"]))
                # A rollback before application is harmless, but an unexpected
                # third value needs review and must keep its private journal.
                if result not in {"UPDATE 1", "DELETE 1"}:
                    row = await self.db.fetchrow("SELECT value FROM app_config WHERE key=$1", key)
                    unchanged = row is None if saved is None else (
                        row is not None and decode_json(row["value"]) == saved["value"])
                    errors |= not unchanged
            except Exception:
                errors = True
        for raw_uid in state.get("users", []):
            try:
                uid = uuid.UUID(raw_uid)
                response = await self.client.get(f"{AUTH}/auth/v1/admin/users/{uid}", headers=self.admin)
                require(response.status_code == 404 or (response.status_code == 200 and
                        response.json().get("user_metadata", {}).get("guard_context_replay") == state["run"]),
                        "cleanup_user_ownership_mismatch")
                if response.status_code == 200:
                    # Fence running work BEFORE deleting FK-owned records. Auth
                    # deletion alone cannot remove vectors outside profile FKs.
                    operation = state.setdefault("cleanup_operations", {}).setdefault(
                        raw_uid, str(uuid.uuid4()))
                    save_journal(journal, state)
                    await self.db.fetchval("SELECT public.begin_subject_deletion($1,$2)",
                                           uid, uuid.UUID(operation))
                    response = await self.client.delete(f"{AUTH}/auth/v1/admin/users/{uid}", headers=self.admin)
                    require(response.status_code in {200, 204}, "synthetic_user_cleanup_failed")
                require(await self.db.fetchval("SELECT count(*) FROM auth.users WHERE id=$1", uid) == 0,
                        "synthetic_user_cleanup_incomplete")
                await self.db.fetchval("SELECT public.delete_user_memories($1)", uid)
            except Exception:
                errors = True
        # Let the deployed privacy worker finish its two-empty-sweep protocol.
        # Keep the journal unless accounts, profile rows AND external vectors
        # are gone and the deletion barrier is complete. Never sweep other UIDs.
        pending = {uuid.UUID(value) for value in state.get("users", [])}
        deadline = time.monotonic() + max(180.0, self.recovery_timeout)
        empty_checks: dict[uuid.UUID, int] = {}
        while pending and time.monotonic() < deadline:
            for uid in tuple(pending):
                try:
                    row = await self.db.fetchrow("""SELECT
                      (SELECT count(*) FROM auth.users WHERE id=$1) +
                      (SELECT count(*) FROM profiles WHERE id=$1) +
                      (SELECT count(*) FROM messages WHERE user_id=$1) +
                      (SELECT count(*) FROM async_jobs WHERE user_id=$1) +
                      (SELECT count(*) FROM vecs.moly_memories_v2
                        WHERE metadata->>'user_id'=$1::text) AS remaining,
                      (SELECT state FROM privacy_subject_barriers WHERE user_id=$1) AS state""", uid)
                    clear = row["remaining"] == 0 and row["state"] == "deleted"
                    empty_checks[uid] = empty_checks.get(uid, 0) + 1 if clear else 0
                    if empty_checks[uid] >= 2:
                        pending.remove(uid)
                except Exception:
                    errors = True
            if pending:
                await asyncio.sleep(2.0)
        errors |= bool(pending)
        if not errors:
            journal.unlink(missing_ok=True)
        self.emit(report(0, "none", "cleanup", 0, "cleanup_incomplete" if errors else None))
        return not errors


async def execute(args: argparse.Namespace, scenarios: list[dict], *, checkout: Path) -> bool:
    # These imports are intentionally after explicit opt-in and private-input
    # validation. Unit tests never import application settings or real clients.
    import asyncpg
    from dotenv import dotenv_values
    import httpx

    env = dict(dotenv_values(args.env_file, interpolate=False))
    dsn = validate_targets(env, args.expected_sha)
    secret = env.get("SUPABASE_SECRET_KEY") or env["SUPABASE_SERVICE_ROLE_KEY"]
    public = env.get("SUPABASE_PUBLISHABLE_KEY") or env["SUPABASE_ANON_KEY"]
    if args.cleanup_state:
        journal = private_file(Path(args.cleanup_state), checkout=checkout)
        state = json.loads(journal.read_text())
        require(state.get("schema") == "guard-context-replay-state-v1" and
                state.get("ref") == REF and isinstance(state.get("users"), list) and
                isinstance(state.get("run"), str) and
                set(state.get("written", {})) <= set(CONFIG_KEYS), "cleanup_state_invalid")
    else:
        journal = Path(args.input).parent / f"guard-replay-{uuid.uuid4().hex}.state.json"
        state = {"schema": "guard-context-replay-state-v1", "ref": REF,
                 "run": uuid.uuid4().hex, "users": [], "saved": {}, "written": {}}
        save_journal(journal, state)
    db = await asyncpg.connect(dsn, statement_cache_size=0, command_timeout=15)
    def emit(row: dict) -> None:
        print(json.dumps(row, ensure_ascii=False), flush=True)
    success = True
    async with httpx.AsyncClient(timeout=45, follow_redirects=False, trust_env=False) as client:
        runner = Runner(db, client, {"apikey": secret, "Authorization": f"Bearer {secret}"},
                        public, args.expected_sha, emit)
        try:
            if not args.cleanup_state:
                await runner.health()
                users = [await runner.synthetic_user(s, state, journal) for s in scenarios]
                await runner.configure(state, journal)
                semaphore = asyncio.Semaphore(2)

                async def run_scenario(scenario: dict, uid: uuid.UUID, token: str) -> bool:
                    async with semaphore:
                        try:
                            recovered = False
                            for step in scenario["steps"]:
                                recovered |= await runner.send(uid, token, scenario, step)
                            require(not scenario.get("require_recovery") or recovered, "pressure_not_exercised")
                            await runner.wait_derived(uid)
                            await runner.derived_safety_sources(uid)
                            await asyncio.sleep(1.0)
                            await runner.derived_safety_sources(uid)
                            return True
                        except Exception as exc:
                            reason = str(exc) if isinstance(exc, ReplayError) else "unexpected_replay_failure"
                            emit(report(scenario["number"], scenario["language"], "unknown", 0, reason))
                            return False

                outcomes = await asyncio.gather(*(run_scenario(s, uid, token)
                    for s, (uid, token) in zip(scenarios, users, strict=True)))
                success = all(outcomes)
        finally:
            try:
                success = await runner.cleanup(state, journal) and success
            finally:
                await db.close()
    return success


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--input")
    parser.add_argument("--cleanup-state")
    parser.add_argument("--expected-sha", default="")
    parser.add_argument("--execute-dev", action="store_true")
    args = parser.parse_args(argv)
    # Suppress third-party request/exception log handlers, which may include
    # credential URLs or data. The API's own existing logs are outside this tool.
    logging.disable(logging.CRITICAL)
    checkout = Path(__file__).resolve().parents[1]
    try:
        require(bool(args.input) != bool(args.cleanup_state), "one_private_input_required")
        scenarios = load_corpus(Path(args.input), checkout=checkout) if args.input else []
        if not args.execute_dev:
            require(not args.cleanup_state, "cleanup_requires_execution_opt_in")
            print(json.dumps(report(0, "none", "validation", 0, None)))
            return 0
        return 0 if asyncio.run(execute(args, scenarios, checkout=checkout)) else 1
    except BaseException as exc:
        reason = str(exc) if isinstance(exc, ReplayError) else "execution_failed"
        print(json.dumps(report(0, "none", "setup", 0, reason)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
