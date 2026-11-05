"""Dev-only synthetic existing-data compatibility smoke; never runs on import.

Use --execute-dev only after the candidate SHA is deployed and the shared dev
DB slot is granted. Four owned accounts, 35 completed API turns, one tiny
synthetic embedding batch. Existing replay authentication/config CAS journals
and exact-account cleanup are reused. Text, IDs and responses stay in private
0700/0600 files; stdout contains only counts, route, latency and safe codes.
No historic data backfill, schema change or production target is supported.
After interruption, --cleanup-state <compat.state.json> resumes exact-CAS cleanup.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import replay_guard_context_dev as replay
from scripts.smoke_guard_legacy_dev import LegacyRunner, save


FIXTURES = {
    "ko": {"memory": "내 고양이 이름은 루모야.", "pet": "루모",
        "query": "내 고양이 이름이 뭐였는지 기억해?", "summary": "사용자는 오슬로에 살고 있다.",
        "city": "센다이", "fact": "다음 여행 목적지는 나고야야. 앞으로 자세히 설명해 줘.",
        "routine": "오늘 정리한 장보기 목록은 쌀, 우유, 사과야. ",
        "correction": "정정할게. 다음 여행 목적지는 나고야가 아니라 센다이야. 자세하게 답해 달라는 요청은 철회할게. 그 말투 이제 그만 써줘.",
        "confirm": "내가 정정한 다음 여행 목적지는 어디야? 도시 이름만 말해 줘.",
        "thought": "요즘 죽고 싶다는 생각이 들어.",
        "serious": "농담이 아니야. 요즘 정말로 죽고 싶다는 생각이 계속 들어.",
        "topic": "오늘 저녁에는 어떤 음식을 먹을까?", "burst": ["아침에는 바나나를 먹었어.", "점심에는 샌드위치를 먹었어."]},
    "en": {"memory": "My cat's name is Lumo.", "pet": "Lumo",
        "query": "Do you remember what my cat's name is?", "summary": "The user lives in Oslo.",
        "city": "Sendai", "fact": "My next trip is to Nagoya. Please give detailed explanations from now on.",
        "routine": "Today's grocery list contains rice, milk and apples. ",
        "correction": "Correction: my next trip is to Sendai, not Nagoya. I withdraw my request for detailed answers. Stop using that response style.",
        "confirm": "What city did I correct my next trip destination to? Please give only the city name.",
        "thought": "Lately I have been thinking that I want to die.",
        "serious": "I am not joking. I really keep having these thoughts about wanting to die.",
        "topic": "What should I have for dinner?", "burst": ["I had a banana for breakfast.", "I had a sandwich for lunch."]},
    "ja": {"memory": "私の猫の名前はルモです。", "pet": "ルモ",
        "query": "私の猫の名前、覚えてる？", "summary": "ユーザーはオスロに住んでいる。",
        "city": "仙台", "fact": "次の旅行先は名古屋だよ。これからは詳しく説明してね。",
        "routine": "今日まとめた買い物リストは米と牛乳とりんごだよ。 ",
        "correction": "訂正するね。次の旅行先は名古屋じゃなくて仙台だよ。詳しく答えてほしいというお願いは取り下げる。その話し方はもうやめてね。",
        "confirm": "訂正した次の旅行先はどこ？都市名だけ教えて。",
        "thought": "最近、死にたいと思うことがある。",
        "serious": "冗談じゃないよ。最近、本当に死にたいってずっと思ってる。",
        "topic": "今日の晩ごはんは何を食べようかな？", "burst": ["朝はバナナを食べたよ。", "昼はサンドイッチを食べたよ。"]},
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, default=str).encode()).hexdigest()


def steps(language):
    f = FIXTURES[language]
    # Two complete ordinary turns cross the existing 4KB scheduling boundary,
    # even in English. Inputs remain below the public 2,000-character limit.
    long_one = (f["fact"] + " " + f["routine"] * 100)[:1980]
    long_two = (f["routine"] * 100)[:1980]
    values = [f["query"], long_one, long_two, f["correction"], f["confirm"],
              f["thought"], f["serious"], f["topic"], *f["burst"]]
    kinds = ["normal"] * 5 + ["safety_check", "safety"] + ["normal"] * 3
    result = [{"text": text, "kind": kind, "allow_recovery": kind == "normal",
               "allow_transient_retry": kind == "normal"}
              for text, kind in zip(values, kinds, strict=True)]
    result[0].update(contains=[f["pet"]], replay=True)
    result[4].update(contains=[f["city"]])
    result[5]["question"] = "thought"
    # These are the exact replies already checked by the established guard
    # oracle. Topic response is also privately retained for independent audit.
    result[7]["absent"] = ["findahelpline", "988", "109", "0120-279-338"]
    return result


class CompatRunner(LegacyRunner):
    """Use the legacy runner's fenced config restoration, with v2 enabled."""

    async def configure(self, state, journal):
        # Same CAS/journal protocol as the existing legacy smoke. Global dev
        # enabling is explicit here; this is not an account cohort override.
        async with self.db.transaction():
            active = await self.db.fetchval("SELECT value FROM app_config "
                "WHERE key='context_retention_active_preparation' FOR UPDATE")
            replay.require(active is None or replay.decode_json(active) is None,
                           "active_projection_must_be_null")
            rows = await self.db.fetch("SELECT key,value,updated_at FROM app_config "
                "WHERE key=ANY($1::text[]) ORDER BY key FOR UPDATE", list(replay.CONFIG_KEYS))
            patch = {"crisis_guard_enabled": True, "crisis_guard_policy_version": replay.POLICY,
                     "crisis_guard_timeout_s": 2.0, "crisis_guard_history_messages": 6,
                     "context_retention_v2_enabled": True}
            state["saved"] = {r["key"]: {"value": replay.decode_json(r["value"]),
                               "updated_at": r["updated_at"].isoformat()} for r in rows}
            stamp = await self.db.fetchval("SELECT transaction_timestamp()")
            state["written"] = {k: {"value": v, "updated_at": stamp.isoformat()} for k, v in patch.items()}
            replay.save_journal(journal, state)
            for key, value in patch.items():
                old = state["saved"].get(key)
                if old is None:
                    changed = await self.db.fetchval("INSERT INTO app_config(key,value,updated_at) "
                        "VALUES($1,$2::jsonb,$3) ON CONFLICT DO NOTHING RETURNING key",
                        key, json.dumps(value), stamp)
                else:
                    changed = await self.db.fetchval("UPDATE app_config SET value=$2::jsonb,updated_at=$3 "
                        "WHERE key=$1 AND value=$4::jsonb AND updated_at=$5 RETURNING key",
                        key, json.dumps(value), stamp, json.dumps(old["value"]),
                        datetime.fromisoformat(old["updated_at"]))
                replay.require(changed == key, "configuration_changed_concurrently")

    async def cleanup(self, state, journal):
        # Keep only owned synthetic identities for a later scoped log review.
        # Successful cleanup removes the mutable recovery journal, not this
        # private 0600 metadata receipt. No token or conversation is included.
        save(journal.parent / "owned-subjects.json", {
            "schema": "context-compat-owned-subjects-v1",
            "run": state["run"], "users": sorted(set(state.get("users", []))),
            "synthetic_only": True,
        })
        # A crash can land on either side of the second (OFF) config commit.
        # Accept only one of this run's two exact CAS receipts, never an
        # unrelated administrator edit. The shared cleanup then restores the
        # original value and removes only journal-owned accounts.
        prior = state.get("retention_off_previous")
        if prior is not None:
            row = await self.db.fetchrow("SELECT value,updated_at FROM app_config "
                "WHERE key='context_retention_v2_enabled'")
            if (row is not None and replay.decode_json(row["value"]) == prior["value"]
                    and row["updated_at"].isoformat() == prior["updated_at"]):
                state["written"]["context_retention_v2_enabled"] = prior
                replay.save_journal(journal, state)
        return await super().cleanup(state, journal)

    async def disable_retention(self, state, journal):
        key = "context_retention_v2_enabled"
        async with self.db.transaction():
            old = state["written"][key]
            stamp = await self.db.fetchval("SELECT transaction_timestamp()")
            # Journal the new restoration CAS before commit, as at initial ON.
            state["retention_off_previous"] = old
            state["written"][key] = {"value": False, "updated_at": stamp.isoformat()}
            replay.save_journal(journal, state)
            changed = await self.db.fetchval("UPDATE app_config SET value='false'::jsonb,updated_at=$2 "
                "WHERE key=$1 AND value=$3::jsonb AND updated_at=$4 RETURNING key",
                key, stamp, json.dumps(old["value"]), datetime.fromisoformat(old["updated_at"]))
            if changed != key:
                # The failed TX did not apply our new CAS; restore its journal.
                state["written"][key] = old
                replay.save_journal(journal, state)
                raise replay.ReplayError("configuration_changed_concurrently")

    async def seed_legacy(self, uid, language, vector, vector_schema):
        from app.services.mem0_ingest import candidate_hash
        f = FIXTURES[language]
        cp_id, contract_id, registry_id, provider_id = (uuid.uuid4() for _ in range(4))
        document = [{"kind": "response_style", "action": "prefer", "condition": "always",
                     "target_tag": "brief", "polarity": "positive", "status": "active"}]
        async with self.db.transaction():
            first = await self.db.fetchval("INSERT INTO messages(user_id,sender,kind,content,activity_date,turn_seq,turn_position) "
                "VALUES($1,'user','normal',$2,CURRENT_DATE,1,1) RETURNING id", uid, f["memory"])
            last = await self.db.fetchval("INSERT INTO messages(user_id,sender,kind,content,activity_date,turn_seq,turn_position) "
                "VALUES($1,'moly','normal',$2,CURRENT_DATE,1,2) RETURNING id", uid, f["summary"])
            await self.db.execute("UPDATE chat_contexts SET last_committed_turn_seq=1,memory_source_watermark=1 "
                                  "WHERE user_id=$1", uid)
            await self.db.execute("UPDATE memory_pipeline_states SET source_through_turn_seq=1,"
                "ingest_through_turn_seq=1,consolidated_through_turn_seq=1,historical_upper_turn_seq=1 "
                "WHERE user_id=$1 AND mode='v2' AND bootstrap_status='ready'", uid)
            await self.db.execute("INSERT INTO conversation_checkpoints(id,user_id,through_message_id,summary,source_hash,version) "
                "VALUES($1,$2,$3,$4,$5,'conversation-checkpoint-summary-v1')", cp_id, uid, last, f["summary"],
                digest({"synthetic": str(cp_id)}))
            await self.db.execute("INSERT INTO user_interaction_contracts(id,user_id,version,locale,document_json,rendered_text,render_hash,status) "
                "VALUES($1,$2,1,$3,$4::jsonb,'Synthetic legacy brief response contract',$5,'published')",
                contract_id, uid, language, json.dumps(document), digest(document))
            await self.db.execute("INSERT INTO mem0_memory_registry(id,user_id,provider,collection_version,provider_memory_id,"
                "source_turn_seq,content_hash,canonical_text,semantic_status,provider_delete_state,schema_version) "
                "VALUES($1,$2,'openai','v2',$3,1,$4,NULL,'active','kept','v1')",
                registry_id, uid, provider_id, candidate_hash(f["memory"]))
            await self.db.execute("INSERT INTO mem0_memory_sources(registry_id,user_id,source_turn_seq,source_message_id,source_sender,"
                "evidence_start_utf8,evidence_end_utf8,source_content_hash,source_occurred_at,source_activity_date,authority,extractor_version) "
                "VALUES($1,$2,1,$3,'user',0,$4,$5,now(),CURRENT_DATE,'explicit_user','synthetic')",
                registry_id, uid, first, len(f["memory"].encode()), hashlib.sha256(f["memory"].encode()).hexdigest())
            # Existing collection only. No collection creation or index DDL.
            await self.db.execute(f'INSERT INTO vecs.moly_memories_v2(id,vec,metadata) '
                f'VALUES($1,$2::{vector_schema}.vector,$3::jsonb)', str(provider_id),
                json.dumps(vector), json.dumps({"user_id": str(uid), "text": f["memory"], "data": f["memory"]}))
        return {"cp": cp_id, "contract": contract_id, "registry": registry_id,
                "provider": provider_id, "old_ids": [first, last], "through": last}

    async def immutable_proof(self, uid, fixture):
        # Lifecycle status/revision/timestamps may legitimately advance. Only
        # the original stored document/source identities and bodies are frozen.
        queries = [
            ("SELECT id,sender,kind,content,turn_seq,turn_position,created_at FROM messages "
             "WHERE user_id=$1 AND id=ANY($2::bigint[]) ORDER BY id", fixture["old_ids"]),
            ("SELECT id,summary,source_hash,version,through_message_id FROM conversation_checkpoints "
             "WHERE user_id=$1 AND id=$2", fixture["cp"]),
            ("SELECT id,locale,version,document_json,rendered_text,render_hash FROM user_interaction_contracts "
             "WHERE user_id=$1 AND id=$2", fixture["contract"]),
            ("SELECT id,provider,collection_version,provider_memory_id,source_turn_seq,content_hash,canonical_text "
             "FROM mem0_memory_registry WHERE user_id=$1 AND id=$2", fixture["registry"]),
            ("SELECT registry_id,source_message_id,source_sender,evidence_start_utf8,evidence_end_utf8,"
             "source_content_hash,authority,source_occurred_at FROM mem0_memory_sources "
             "WHERE user_id=$1 AND registry_id=$2 AND source_turn_seq=1 ORDER BY source_message_id", fixture["registry"]),
        ]
        groups = []
        for query, identity in queries:
            rows = await self.db.fetch(query, uid, identity)
            replay.require(bool(rows), "legacy_fixture_row_missing")
            groups.append([dict(row) for row in rows])
        replay.require(groups[3][0]["canonical_text"] is None, "legacy_canonical_was_rewritten")
        return digest(groups)

    async def verify_vector(self, uid, language, fixture, vector, vector_schema, app_db):
        from app.services import mem0_adapter, mem0_recall, provided_memory
        db = self.db

        class ActualVectorSearch:
            async def search(self, embedding, *, user_id, limit=25, timeout=5):
                rows = await db.fetch(f'SELECT id,metadata,vec OPERATOR({vector_schema}.<=>) '
                    f'$2::{vector_schema}.vector AS distance FROM vecs.moly_memories_v2 '
                    f'WHERE metadata->>\'user_id\'=$1 ORDER BY vec OPERATOR({vector_schema}.<=>) '
                    f'$2::{vector_schema}.vector LIMIT $3', user_id, json.dumps(embedding), limit,
                    timeout=timeout)
                return [mem0_adapter.SearchHit(str(r["id"]), float(r["distance"]),
                            replay.decode_json(r["metadata"])) for r in rows]

        async def embed_query(_query, *, timeout=None):
            return vector  # A real provider embedding obtained by the one fixture batch.

        async with app_db.get_sessionmaker()() as session:
            found = await mem0_recall.recall(session, uid, query=FIXTURES[language]["query"],
                adapter=ActualVectorSearch(), embed_query=embed_query)
            replay.require(any(r.provider_memory_id == str(fixture["provider"]) for r in found),
                           "legacy_real_vector_hit_missing")
            proof = await provided_memory.capture(session, user_id=uid, generation=0,
                ids=[fixture["registry"]], language=language,
                canonical_overrides={fixture["registry"]: FIXTURES[language]["memory"]})
            replay.require(len(proof) == 1 and proof[0].canonical_text == FIXTURES[language]["memory"],
                           "legacy_null_canonical_not_certified")

    async def baseline(self, uid):
        value = await self.db.fetchval("SELECT retention_compat_json FROM chat_contexts WHERE user_id=$1", uid)
        value = replay.decode_json(value)
        replay.require(isinstance(value, dict), "compat_baseline_not_captured")
        return value

    async def wait_native_checkpoint(self, uid, old_through, app_db):
        from app.services import checkpoint_repo, context_compat
        from app.services.retention_preparation_context import use_binding
        end = time.monotonic() + self.recovery_timeout
        while time.monotonic() < end:
            # Exact owned ready attempt-0 scheduling acceleration only. This is
            # a correctness/drain test, never a production scheduling latency claim.
            await self.expedite_owned_ingest(uid)
            async with app_db.get_sessionmaker()() as session:
                with use_binding(None):
                    cp = await checkpoint_repo.load_latest(session, uid, retention_enabled=True)
                    if cp is not None:
                        b = await context_compat.load(session, uid, 0)
                        coverage = await checkpoint_repo.coverage_for_checkpoint(session, user_id=uid,
                            generation=0, value=cp)
                        replay.require(b is not None and b.source_after == old_through,
                                       "compat_baseline_lower_bound_changed")
                        replay.require(cp.validation_json.get("baseline_binding") == b.payload(),
                                       "checkpoint_baseline_binding_mismatch")
                        replay.require(coverage is not None and coverage.after_message_id == b.source_after,
                                       "checkpoint_granted_legacy_prefix")
                        ids = await self.db.fetch("SELECT id FROM messages WHERE user_id=$1 AND id>$2 AND id<=$3 ORDER BY id",
                                                 uid, b.source_after, cp.through_message_id)
                        published = await self.db.fetch("SELECT validation_json FROM conversation_checkpoints "
                            "WHERE user_id=$1 AND version='conversation-checkpoint-summary-v5' AND publish_state='published' "
                            "AND preparation_id IS NULL AND through_message_id<=$2", uid, cp.through_message_id)
                        covered = set()
                        for row in published:
                            validation = replay.decode_json(row["validation_json"])
                            replay.require(validation.get("baseline_binding") == b.payload(), "old_prefix_reprocessed")
                            source_ids = validation.get("source_message_ids", [])
                            replay.require(all(type(mid) is int and mid > b.source_after for mid in source_ids),
                                           "old_prefix_reprocessed")
                            covered.update(source_ids)
                        replay.require(covered == {r["id"] for r in ids}, "checkpoint_source_gap")
                        return cp.through_message_id
            await asyncio.sleep(0.5)
        raise replay.ReplayError("native_checkpoint_publication_timeout")

    async def verify_raw_tail(self, uid, old_through, app_db, language):
        from app.services import chat, checkpoint_repo
        from app.services.retention_preparation_context import use_binding
        async with app_db.get_sessionmaker()() as session:
            with use_binding(None):
                cp = await checkpoint_repo.load_latest(session, uid, retention_enabled=True)
                replay.require(cp is not None, "native_checkpoint_missing")
                coverage = await checkpoint_repo.coverage_for_checkpoint(session, user_id=uid,
                    generation=0, value=cp)
                replay.require(coverage is not None, "native_checkpoint_coverage_invalid")
                window = await chat._context(session, uid, 0, language=language, retention_enabled=True,
                    memory_generation=0, coverage=coverage, compact_crises=False, legacy_source_after=old_through)
                represented = {entry.source_ref.message_id for entry in window.conversation
                               if getattr(entry, "source_ref", None) is not None}
                represented.update(m.id for m in window.lead)
                expected = await self.db.fetch("SELECT id FROM messages WHERE user_id=$1 AND id>$2 ORDER BY id",
                                              uid, cp.through_message_id)
                replay.require({row["id"] for row in expected} <= represented, "uncovered_raw_omitted")
        expected_proof = self.raw_proofs.get(uid, {})
        replay.require(await self.raw_message_proofs(uid, list(expected_proof)) == expected_proof,
                       "committed_source_content_changed")

    async def wait_owned_facts(self, uid):
        # Drain only normal turns already committed by this account. Old NULL
        # canonical records are read-compatible, not retroactive processing jobs.
        target = await self.db.fetchval("SELECT max(turn_seq) FROM messages WHERE user_id=$1 AND kind='normal'", uid)
        end = time.monotonic() + self.recovery_timeout
        while time.monotonic() < end:
            await self.expedite_owned_ingest(uid)
            done = await self.db.fetchval("SELECT consolidated_through_turn_seq >= $2 "
                "FROM memory_pipeline_states WHERE user_id=$1 AND mode='v2' AND bootstrap_status='ready'", uid, target)
            pending = await self.db.fetchval("SELECT EXISTS(SELECT 1 FROM async_jobs WHERE user_id=$1 "
                "AND job_type IN ('mem0_ingest','consolidate','contract_compile') AND state IN ('ready','running'))", uid)
            if done is True and not pending:
                return
            await asyncio.sleep(0.5)
        raise replay.ReplayError("owned_suffix_fact_drain_timeout")

    async def burst(self, uid, token, scenario, values):
        """Only HTTP requests run concurrently; the shared DB connection does not."""
        before = await self.snapshot(uid)
        expected = [{"text": value, "locale": scenario["language"]} for value in values]
        keys = [f"compat-burst-{uuid.uuid4().hex}" for _ in expected]

        async def one(index):
            body, key = expected[index], keys[index]
            headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}
            started = time.monotonic()
            retries = 0
            while time.monotonic() - started < self.recovery_timeout:
                response = await self.client.post(replay.API + "/chat/messages", headers=headers, json=body)
                if response.status_code == 200:
                    replay.check_reply(response.json(), {"kind": "normal"}, scenario["language"])
                    return response.json(), headers, retries, round((time.monotonic() - started) * 1000)
                failure = replay.response_failure(response)
                replay.require((response.status_code == 409 and failure in {
                    "http_409_chat_turn_in_progress", "http_409_chat_busy", "http_409_chat_turn_stale"})
                    or (response.status_code == 503 and failure == "http_503_ai_unavailable"), failure)
                retries += 1
                await asyncio.sleep(0.5)
            raise replay.ReplayError("burst_busy_retry_timeout")

        # return_exceptions waits for both tasks before verification/cleanup;
        # an early failure must never leave a sibling writing during cleanup.
        results = await asyncio.gather(one(0), one(1), return_exceptions=True)
        for item in results:
            if isinstance(item, BaseException):
                raise item
        after = await self.snapshot(uid)
        replay.require(after[0] == before[0] + 4, "burst_message_count_mismatch")
        all_ids = []
        for index, (payload, headers, retries, latency) in enumerate(results):
            ids = [int(payload["user_message"]["message_id"]), int(payload["reply"]["message_id"])]
            from app.services.message_content import project_message
            rows = [project_message(dict(row), user_id=uid) for row in await self.db.fetch(
                "SELECT id,sender,kind,content,content_ciphertext,content_chars FROM messages "
                "WHERE user_id=$1 AND id=ANY($2::bigint[])", uid, ids)]
            replay.require(len(rows) == 2 and all(r["kind"] == "normal" for r in rows)
                and any(r["sender"] == "user" and r["content"] == values[index] for r in rows), "burst_source_mismatch")
            all_ids.extend(ids)
            response = await self.client.post(replay.API + "/chat/messages", headers=headers, json=expected[index])
            replay.require(response.status_code == 200 and response.json() == payload, "burst_replay_result_changed")
            self.emit({**replay.report(scenario["number"], scenario["language"], "normal", latency, None),
                       "turn": 9 + index, "busy_or_pressure_retries": retries})
        replay.require(await self.snapshot(uid) == after, "burst_replay_duplicate_side_effects")
        self.raw_proofs.setdefault(uid, {}).update(await self.raw_message_proofs(uid, all_ids))


async def run(args):
    import asyncpg
    from dotenv import dotenv_values
    import httpx
    replay.private_file(Path(args.env_file), checkout=Path(__file__).resolve().parents[1])
    env = dict(dotenv_values(args.env_file, interpolate=False))
    dsn = replay.validate_targets(env, args.expected_sha)
    if args.cleanup_state:
        journal = replay.private_file(Path(args.cleanup_state), checkout=Path(__file__).resolve().parents[1])
        state = json.loads(journal.read_text())
        replay.require(state.get("schema") == "guard-context-replay-state-v1" and state.get("ref") == replay.REF
            and isinstance(state.get("users"), list) and isinstance(state.get("run"), str)
            and set(state.get("written", {})) <= set(replay.CONFIG_KEYS), "cleanup_state_invalid")
        directory = journal.parent
    else:
        replay.require(bool(args.output_directory), "private_output_required")
        directory = Path(args.output_directory).absolute()
        replay.require(not directory.exists() and not any(p.is_symlink() or (p / ".git").exists() for p in directory.parents),
                       "private_output_must_be_new_outside_git")
        directory.mkdir(mode=0o700)
        state = {"schema": "guard-context-replay-state-v1", "ref": replay.REF,
                 "run": uuid.uuid4().hex, "users": [], "saved": {}, "written": {}}
        journal = directory / "compat.state.json"
        replay.save_journal(journal, state)
    os.environ["MOLY_ENV_FILE"] = str(Path(args.env_file).resolve())
    from db.envfile import configure_application_db
    configure_application_db(args.env_file)
    from app.core import db as app_db
    from app.services import memory_embeddings
    from worker.privacy_sweep_jobs import _sweep_subject
    reports, responses = [], []

    def emit(value):
        reports.append(value)
        save(directory / "results.json", reports)
        print(json.dumps(value), flush=True)

    class CaptureClient(httpx.AsyncClient):
        async def post(self, url, **kwargs):
            response = await super().post(url, **kwargs)
            if str(url) == replay.API + "/chat/messages":
                responses.append({"http": response.status_code,
                    "input": kwargs.get("json"), "response": response.json() if response.status_code == 200 else None})
                save(directory / "synthetic-responses.json", responses)
            return response

    db = await asyncpg.connect(dsn, statement_cache_size=0, command_timeout=15)
    success, cleaned, failure = False, False, None
    clients = []
    try:
        async with CaptureClient(timeout=45, follow_redirects=False, trust_env=False) as client:
            secret = env.get("SUPABASE_SECRET_KEY") or env["SUPABASE_SERVICE_ROLE_KEY"]
            runner = CompatRunner(db, client, {"apikey": secret, "Authorization": "Bearer " + secret},
                env.get("SUPABASE_PUBLISHABLE_KEY") or env["SUPABASE_ANON_KEY"], args.expected_sha, emit,
                run_tag=state["run"], privacy_step=_sweep_subject, recovery_timeout=args.drain_timeout)
            if args.cleanup_state:
                cleaned = await runner.cleanup(state, journal)
                print(json.dumps({"cleanup": cleaned, "success": cleaned}), flush=True)
                return 0 if cleaned else 1
            try:
                await runner.health()
                schema = await db.fetchval("SELECT n.nspname FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
                                           "WHERE t.typname='vector'")
                replay.require(schema in {"public", "extensions"}, "vector_type_schema_unexpected")
                # Three old facts + three recall queries, all synthetic. No chat
                # completion or all-history embedding is performed by setup.
                languages = list(FIXTURES)
                vectors = await memory_embeddings.embed_texts(
                    [FIXTURES[lang]["memory"] for lang in languages] + [FIXTURES[lang]["query"] for lang in languages], timeout=20)
                for index, language in enumerate(languages):
                    scenario = {"number": index + 1, "language": language, "require_checkpoint": True}
                    uid, token = await replay.Runner.synthetic_user(runner, scenario, state, journal)
                    fixture = await runner.seed_legacy(uid, language, vectors[index], schema)
                    proof = await runner.immutable_proof(uid, fixture)
                    await runner.verify_vector(uid, language, fixture, vectors[index + 3], schema, app_db)
                    clients.append((scenario, uid, token, fixture, proof))
                await runner.configure(state, journal)
                for scenario, uid, token, fixture, old_proof in clients:
                    values = steps(scenario["language"])
                    baseline_hash = None
                    for index, step in enumerate(values[:8]):
                        await runner.send(uid, token, scenario, step)
                        b = await runner.baseline(uid)
                        if baseline_hash is None:
                            replay.require(b["source_after"] == fixture["through"]
                                and b["legacy_checkpoint_id"] == str(fixture["cp"])
                                and b["legacy_contract_id"] == str(fixture["contract"]), "legacy_baseline_identity_mismatch")
                            baseline_hash = digest(b)
                        replay.require(digest(b) == baseline_hash, "immutable_baseline_changed")
                        if index == 2:
                            await runner.wait_native_checkpoint(uid, fixture["through"], app_db)
                    await runner.burst(uid, token, scenario, [v["text"] for v in values[8:]])
                    await runner.wait_native_checkpoint(uid, fixture["through"], app_db)
                    await runner.verify_raw_tail(uid, fixture["through"], app_db, scenario["language"])
                    await runner.wait_owned_facts(uid)
                    await runner.derived_safety_sources(uid)
                    replay.require(await runner.immutable_proof(uid, fixture) == old_proof, "legacy_stored_body_changed")
                    replay.require(digest(await runner.baseline(uid)) == baseline_hash, "immutable_baseline_changed")
                    replay.require(not await db.fetchval("SELECT EXISTS(SELECT 1 FROM async_jobs WHERE user_id=$1 "
                        "AND job_type='mem0_ingest' AND (payload->>'turn_seq')::bigint<=1)", uid), "old_turn_reingested")
                # A fourth short conversation has a baseline but no native v5
                # yet. Its missing pipeline exercises bounded first enrollment,
                # without replaying or canonicalizing its old stored turn.
                scenario = {"number": 4, "language": "ko"}
                uid, token = await replay.Runner.synthetic_user(runner, scenario, state, journal)
                fixture = await runner.seed_legacy(uid, "ko", vectors[0], schema)
                old_proof = await runner.immutable_proof(uid, fixture)
                clients.append((scenario, uid, token, fixture, old_proof))
                await db.execute("DELETE FROM memory_pipeline_states WHERE user_id=$1", uid)
                await runner.send(uid, token, scenario, {"text": "오늘 아침에는 우유를 마셨어.",
                    "kind": "normal", "replay": True, "allow_transient_retry": True})
                baseline_only = digest(await runner.baseline(uid))
                replay.require(await db.fetchval("SELECT count(*) FROM conversation_checkpoints WHERE user_id=$1 "
                    "AND version='conversation-checkpoint-summary-v5'", uid) == 0, "short_fixture_unexpected_v5")
                await runner.disable_retention(state, journal)
                for scenario, uid, token, fixture, old_proof in clients:
                    b_before = digest(await runner.baseline(uid))
                    cp_before = await db.fetchval("SELECT count(*) FROM conversation_checkpoints WHERE user_id=$1 "
                        "AND version='conversation-checkpoint-summary-v5'", uid)
                    await runner.send(uid, token, scenario, {"text": FIXTURES[scenario["language"]]["confirm"]
                        if scenario["number"] != 4 else FIXTURES["ko"]["query"], "kind": "normal", "replay": True,
                        "contains": [FIXTURES[scenario["language"]]["city"] if scenario["number"] != 4 else FIXTURES["ko"]["pet"]],
                        "allow_transient_retry": True})
                    replay.require(digest(await runner.baseline(uid)) == b_before, "off_replaced_baseline")
                    replay.require(await runner.immutable_proof(uid, fixture) == old_proof, "off_rewrote_legacy_body")
                    replay.require(await db.fetchval("SELECT count(*) FROM conversation_checkpoints WHERE user_id=$1 "
                        "AND version='conversation-checkpoint-summary-v5'", uid) == cp_before, "v5_published_after_off")
                    if scenario["number"] == 4:
                        replay.require(b_before == baseline_only and cp_before == 0, "baseline_only_fixture_changed")
                success = True
            except Exception as error:
                failure = str(error) if isinstance(error, replay.ReplayError) else type(error).__name__.lower()
            finally:
                cleaned = await runner.cleanup(state, journal)
    finally:
        await db.close()
        if app_db._engine is not None:
            await app_db._engine.dispose()
    summary = {"schema": "context-compat-dev-smoke-v1", "success": success and cleaned,
        "failure": failure, "cleanup": cleaned, "accounts": len(clients),
        "expected_completed_turns": 35,
        "completed_turn_checks": sum(r["failure"] is None and r.get("scenario", 0) > 0
                                     and r.get("route") in replay.KINDS for r in reports),
        "completed_api_turns": len({item["response"]["user_message"]["message_id"]
                                    for item in responses if item["http"] == 200}),
        "http_attempts": len(responses), "global_guard": True, "global_retention": True,
        "active_preparation": None, "retention_off_read_compatibility_accounts": 4,
        "legacy_null_canonical_real_vector_fixture": True,
        "new_turn_worker_publication_required": True, "owned_ingest_scheduling_acceleration": True,
        "scheduling_latency_benchmark": False}
    save(directory / "summary.json", summary)
    print(json.dumps(summary), flush=True)
    return 0 if summary["success"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--output-directory")
    parser.add_argument("--cleanup-state", help="Resume this harness journal without creating users or calling models")
    parser.add_argument("--drain-timeout", type=float, default=180)
    parser.add_argument("--execute-dev", action="store_true")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        replay.require(args.execute_dev, "explicit_dev_execution_required")
        replay.require(0 < args.drain_timeout <= 300, "drain_timeout_out_of_bounds")
        return asyncio.run(run(args))
    except BaseException as error:
        reason = str(error) if isinstance(error, replay.ReplayError) else type(error).__name__.lower()
        print(json.dumps({"success": False, "failure": reason}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
