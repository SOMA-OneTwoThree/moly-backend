"""All-user maintenance preparation; default is a read-only plan, never a rollout.

No model/provider API, DDL, source edits, cursor edits or dead-job replay. Run
after the reviewed migration. Keep the API closed until a fresh audit is ready.
Plans contain aggregate counts and fingerprints, not account IDs or source text.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FORMAT = "guard-context-release-v1"
MAINTENANCE_ACK = "PUBLIC_CHAT_CLOSED_AND_OLD_WORKERS_DRAINED"
PROD_PROJECT_REF = "qkgjlgzsharnilxnkytd"
REQUIRED_COLUMNS = {
    ("mem0_memory_registry", "canonical_text"),
    ("mem0_memory_registry", "context_priority"),
    ("mem0_ingest_candidates", "context_priority"),
    ("chat_contexts", "contract_generation"),
    ("chat_contexts", "contract_processed_through_message_id"),
    ("chat_contexts", "retention_pressure_through_message_id"),
    ("conversation_checkpoints", "state_json"),
    ("conversation_checkpoints", "validation_json"),
    ("conversation_checkpoints", "previous_checkpoint_id"),
}


class Blocked(ValueError):
    """Fixed safe reason only: never attach database values or exception strings."""


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


def require_target(env, dsn, expected_ref, production_ack=False):
    from db.envfile import DEV_PROJECT_REFS
    if not re.fullmatch(r"[a-z0-9]{20}", expected_ref or ""):
        raise Blocked("expected_project_ref_required")
    try:
        u = urlsplit(dsn)
        user, host = unquote(u.username or ""), u.hostname or ""
        direct = host == f"db.{expected_ref}.supabase.co" and user == "postgres"
        pooler = (host.endswith(".pooler.supabase.com")
                  and user == f"postgres.{expected_ref}")
        valid = (u.scheme in {"postgres", "postgresql", "postgresql+asyncpg"}
                 and not u.query and not u.fragment and u.path == "/postgres"
                 and u.port in {5432, 6543} and (direct or pooler))
    except ValueError:
        valid = False
    if not valid or env not in {"dev", "prod"}:
        raise Blocked("database_target_mismatch")
    if (env == "dev" and expected_ref not in DEV_PROJECT_REFS) or (
            env == "prod" and (expected_ref != PROD_PROJECT_REF or not production_ack)):
        raise Blocked("explicit_environment_required")


def reserve_private(path: Path):
    """Create once outside Git, refusing symlinks and unsafe output parents."""
    path = path.absolute()
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise Blocked("unsafe_output_path")
    if any((p / ".git").exists() for p in path.parents):
        raise Blocked("output_must_be_outside_git")
    if not path.parent.is_dir():
        raise Blocked("output_parent_missing")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "w+")


def write_private(stream, value):
    stream.seek(0)
    json.dump(value, stream, sort_keys=True, ensure_ascii=False)
    stream.write("\n")
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


_USERS = text("""
SELECT p.id AS user_id,p.language,p.timezone,cc.memory_generation,cc.last_committed_turn_seq,
       COALESCE(b.state,'missing') AS barrier_state,COALESCE(b.epoch,0) AS barrier_epoch,
       s.mode,s.bootstrap_status,s.privacy_epoch,s.repair_generation
FROM profiles p LEFT JOIN chat_contexts cc ON cc.user_id=p.id
LEFT JOIN privacy_subject_barriers b ON b.user_id=p.id
LEFT JOIN memory_pipeline_states s ON s.user_id=p.id ORDER BY p.id
""")
_MESSAGES = """SELECT id,sender,kind,content,created_at,turn_seq,turn_position
FROM messages WHERE user_id=:uid ORDER BY id"""
_MEMORIES = """
SELECT r.id,r.provider,r.collection_version,r.provider_memory_id,r.content_hash,
 r.canonical_text,
 ARRAY(SELECT DISTINCT c.candidate_text FROM mem0_ingest_candidates c
       WHERE c.user_id=r.user_id AND c.provider_memory_id=r.provider_memory_id
         AND c.candidate_hash=r.content_hash AND c.status='committed') AS candidates,
 v.metadata->>'user_id' AS vector_owner,v.metadata->>'data' AS vector_data,
 v.metadata->>'text' AS vector_text
FROM mem0_memory_registry r
LEFT JOIN vecs.moly_memories_v2 v ON v.id=r.provider_memory_id::text
WHERE r.user_id=:uid AND r.semantic_status='active' AND r.provider_delete_state='kept'
ORDER BY r.id
"""
_SOURCES = """
SELECT s.registry_id,s.source_message_id,s.source_sender,s.authority,
 s.evidence_start_utf8 AS start_utf8,s.evidence_end_utf8 AS end_utf8,
 s.source_content_hash AS span_hash,s.source_occurred_at,
 m.id AS actual_message_id,m.sender AS message_sender,m.kind AS message_kind,
 m.content AS message_content
FROM mem0_memory_sources s LEFT JOIN messages m
 ON m.id=s.source_message_id AND m.user_id=s.user_id
WHERE s.user_id=:uid AND s.registry_id=ANY(CAST(:ids AS uuid[]))
ORDER BY s.registry_id,s.source_message_id,s.evidence_start_utf8,s.evidence_end_utf8
"""
_BUSY = text("""SELECT
 EXISTS(SELECT 1 FROM chat_active_turns WHERE lease_until>=now()) OR
 EXISTS(SELECT 1 FROM async_jobs WHERE state='running'
  AND job_type IN ('conversation_checkpoint','contract_compile','mem0_ingest',
                  'mem0_consolidate','mem0_reconsolidate'))""")
_CAS = text("""UPDATE mem0_memory_registry SET canonical_text=:canonical
WHERE id=:id AND user_id=:uid AND provider_memory_id=:provider_memory_id
 AND provider=:provider AND collection_version=:collection_version AND content_hash=:content_hash
 AND semantic_status='active' AND provider_delete_state='kept' AND canonical_text IS NULL
RETURNING id""")


def certified_canonical(row, owner):
    from app.services.mem0_ingest import candidate_hash, normalize
    values = list(row.get("candidates") or ())
    if row.get("vector_owner") == str(owner):
        values.extend((row.get("vector_data"), row.get("vector_text")))
    approved = {normalize(v) for v in values
                if isinstance(v, str) and normalize(v) and candidate_hash(v) == row["content_hash"]}
    if len(approved) != 1:
        raise Blocked("canonical_unproved")
    canonical = next(iter(approved))
    existing = row.get("canonical_text")
    if existing is not None:
        if not isinstance(existing, str) or normalize(existing) != canonical:
            raise Blocked("existing_canonical_conflict")
        canonical = existing
    return canonical


@dataclass
class UserPlan:
    user: dict
    messages: list
    memories: list
    canonical: dict
    through: int
    fingerprint: str


def authorize_metadata(user):
    """Existing privacy authorization precedes every source/vector/body query."""
    from app.config import settings
    from app.services import privacy
    state = user["barrier_state"] or privacy.STATUS_MISSING
    if state in {privacy.STATUS_DELETING, privacy.STATUS_DELETED}:
        return False
    if state not in {privacy.STATUS_ACTIVE, privacy.STATUS_MISSING}:
        raise Blocked("subject_authorization_unknown")
    epoch = user["barrier_epoch"] or 0
    payload_epoch = user["privacy_epoch"] if user["privacy_epoch"] is not None else epoch
    allowed = privacy.authorize_job(privacy.BarrierState(state, epoch, None),
        job_type="conversation_checkpoint", payload_epoch=payload_epoch,
        mode=settings.privacy_barrier_mode).allowed
    if not allowed:
        raise Blocked("historical_privacy_not_ready")
    return True


async def read_user(session, user, *, lock=False):
    from app.services.provided_memory import _source
    if not authorize_metadata(user):
        raise Blocked("subject_not_authorized")
    uid = user["user_id"]
    messages = [dict(r) for r in (await session.execute(
        text(_MESSAGES + (" FOR SHARE" if lock else "")), {"uid": uid})).mappings().all()]
    memories = [dict(r) for r in (await session.execute(text(
        _MEMORIES + (" FOR UPDATE OF r" if lock else "")), {"uid": uid})).mappings().all()]
    ids = [r["id"] for r in memories]
    if lock and ids:
        await session.execute(text("""SELECT registry_id FROM mem0_memory_sources
          WHERE user_id=:uid AND registry_id=ANY(CAST(:ids AS uuid[]))
          ORDER BY registry_id,source_message_id,evidence_start_utf8,evidence_end_utf8 FOR SHARE"""),
          {"uid": uid, "ids": ids})
    sources = (await session.execute(text(_SOURCES), {"uid": uid, "ids": ids})).mappings().all() if ids else []
    evidence = {identity: [] for identity in ids}
    for source in sources:
        proof = _source(source)
        evidence[source["registry_id"]].append(digest([
            proof.message_id, proof.source_sender, proof.authority, proof.start_utf8,
            proof.end_utf8, proof.span_hash, proof.raw_content_hash, proof.kind,
            proof.message_sender, proof.source_occurred_at]))
    canonical = {}
    for row in memories:
        if not evidence[row["id"]]:
            raise Blocked("canonical_source_missing")
        canonical[row["id"]] = certified_canonical(row, uid)
    normal = [m for m in messages if m["kind"] == "normal"]
    if normal and (user["memory_generation"] is None or user["mode"] != "v2"
                   or user["bootstrap_status"] != "ready"):
        raise Blocked("historical_pipeline_not_ready")
    if any(m["turn_seq"] is None for m in normal):
        raise Blocked("normal_turn_unsequenced")
    if normal and user["memory_generation"] != 0:
        # A nonzero generation can close earlier raw history. This one-time
        # migration does not invent a new source boundary for a forgotten era.
        raise Blocked("generation_scope_unproved")
    user_turns = {}
    for message in messages:
        if message["sender"] == "user" and message["turn_position"] == 1:
            user_turns.setdefault(message["turn_seq"], message["id"])
    complete = [m for m in messages if m["sender"] == "moly" and m["turn_position"] == 2
                and m["turn_seq"] is not None and user_turns.get(m["turn_seq"], m["id"]) < m["id"]]
    complete_turns = {m["turn_seq"] for m in complete}
    if any(m["turn_seq"] not in complete_turns for m in normal):
        raise Blocked("incomplete_normal_turn")
    through = max((m["id"] for m in complete), default=0)
    if normal and (not complete or max(m["turn_seq"] for m in complete)
                   > (user["last_committed_turn_seq"] or 0)):
        raise Blocked("committed_boundary_missing")
    fingerprint = digest({"owner": str(uid), "generation": user["memory_generation"],
        "epoch": user["barrier_epoch"], "privacy_state": user["barrier_state"],
        "pipeline": [user["mode"], user["bootstrap_status"], user["privacy_epoch"]],
        "repair_generation": user["repair_generation"],
        "messages": messages, "through": through,
        "memories": [[str(r["id"]), r["provider"], r["collection_version"],
                      str(r["provider_memory_id"]), r["content_hash"], digest(canonical[r["id"]]),
                      sorted(evidence[r["id"]])] for r in memories]})
    return UserPlan(user, messages, memories, canonical, through, fingerprint)


async def schema_ready(session):
    rows = (await session.execute(text("""SELECT table_name,column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name IN
        ('mem0_memory_registry','mem0_ingest_candidates','chat_contexts','conversation_checkpoints')"""))).all()
    return REQUIRED_COLUMNS.issubset({tuple(row) for row in rows})


async def collect(session):
    if not await schema_ready(session):
        raise Blocked("reviewed_migration_required")
    users = [dict(row) for row in (await session.execute(_USERS)).mappings().all()]
    plans, blockers, excluded = [], Counter(), []
    for user in users:
        try:
            if not authorize_metadata(user):
                excluded.append([str(user["user_id"]), user["barrier_state"],
                                 user["barrier_epoch"], user["memory_generation"]])
                continue
            plans.append(await read_user(session, user))
        except (Blocked, ValueError) as exc:
            blockers[str(exc) if isinstance(exc, Blocked) else "source_proof_invalid"] += 1
    return plans, blockers, len(users), excluded


def artifact(plans, blockers, population, *, project_ref, excluded=()):
    return {"format": FORMAT, "project_ref": project_ref, "population": population,
            "eligible_users": population-len(excluded),
            "excluded_authorization": dict(Counter(row[1] for row in excluded)),
            "prepared_users": len(plans), "history_users": sum(bool(p.through) for p in plans),
            "active_memories": sum(len(p.memories) for p in plans),
            "missing_canonical": sum(r["canonical_text"] is None for p in plans for r in p.memories),
            "blockers": dict(sorted(blockers.items())),
            "source_fingerprint": digest({"eligible": [p.fingerprint for p in plans],
                                          "excluded_metadata": excluded})}


def approved_plan(plan, current, checksum):
    if digest(plan) != checksum or plan.get("format") != FORMAT or plan.get("blockers"):
        raise Blocked("plan_not_approved")
    # NULL->the exact certified value and domain jobs do not change this binding.
    for key in ("project_ref", "population", "eligible_users", "excluded_authorization",
                "prepared_users", "history_users",
                "active_memories", "source_fingerprint"):
        if plan.get(key) != current.get(key):
            raise Blocked("plan_source_changed")
    if current.get("blockers"):
        raise Blocked("current_source_blocked")


async def prepare_user(session, expected):
    from app.core.advisory_lock import advisory_xact_lock
    from app.services import checkpoint_repo, context_retention
    uid = expected.user["user_id"]
    await session.execute(text("SET LOCAL lock_timeout='250ms'"))
    await advisory_xact_lock(session, uid)
    await session.execute(text("SELECT user_id FROM chat_contexts WHERE user_id=:uid FOR UPDATE"), {"uid": uid})
    epoch = expected.user["barrier_epoch"]
    if expected.through and (not await context_retention.enabled_now(session, lock=True)
        or not await context_retention.privacy_epoch_matches(session, user_id=uid, privacy_epoch=epoch, lock=True)):
        raise Blocked("global_worker_or_privacy_not_ready")
    fresh = (await session.execute(text(str(_USERS).replace("ORDER BY p.id", "WHERE p.id=:uid")),
                                   {"uid": uid})).mappings().first()
    user = dict(fresh) if fresh is not None else None
    if user is None:
        raise Blocked("plan_source_changed")
    current = await read_user(session, user, lock=True)
    if current.fingerprint != expected.fingerprint:
        raise Blocked("plan_source_changed")
    for row in current.memories:
        if row["canonical_text"] is None:
            params = {**row, "uid": uid, "canonical": current.canonical[row["id"]]}
            if (await session.execute(_CAS, params)).first() is None:
                raise Blocked("canonical_cas_failed")
    if current.through:
        await context_retention.enqueue_contract_processing(session, user_id=uid,
            generation=user["memory_generation"], through_id=current.through, privacy_epoch=epoch)
        reserved = await checkpoint_repo.request_pressure(session, user_id=uid,
            generation=user["memory_generation"], through_id=current.through)
        if reserved.status not in {"queued", "pending", "covered"}:
            raise Blocked("preparation_reservation_failed")


def reverified_matches(current, parent, messages):
    """Mirror the existing reverify publisher's distinct, whole-history binding."""
    from app.models.message import SUMMARY_EXCLUDED_MESSAGE_KINDS
    from app.services import checkpoint, context_retention
    if (parent is None or current["through_message_id"] != parent["through_message_id"]
            or current["coverage_from_message_id"] != 1
            or current["coverage_through_message_id"] != current["through_message_id"]
            or current["segment_through_message_id"] != current["through_message_id"]):
        return False
    manifests = current["validation_json"].get("page_manifests")
    if not isinstance(manifests, list) or not manifests:
        return False
    after, increments = 0, 0
    for manifest in manifests:
        if (not isinstance(manifest, dict) or set(manifest) != {"after", "through", "hash"}
                or manifest["after"] != after or type(manifest["through"]) is not int
                or not after < manifest["through"] <= current["through_message_id"]):
            return False
        page = [m for m in messages if after < m["id"] <= manifest["through"]]
        if not page or len(page) > 120 or page[-1]["id"] != manifest["through"]:
            return False
        sources = [checkpoint.SourceMessage(m["id"], m["sender"], m["kind"], m["content"] or "",
                                           m["created_at"]) for m in page]
        if checkpoint.source_hash(previous=None, messages=sources,
                                  version=current["version"]) != manifest["hash"]:
            return False
        increments += any(m["kind"] not in SUMMARY_EXCLUDED_MESSAGE_KINDS for m in page)
        after = manifest["through"]
    if after != current["through_message_id"]:
        return False
    state = context_retention.validate_state(current["state_json"])
    old = context_retention.validate_state(parent["state_json"])
    items = {item["id"]: item for item in state["items"]}
    if (state["sequence"] != old["sequence"] + increments
            or any(items.get(item["id"]) != item for item in old["items"])):
        return False
    bound = hashlib.sha256(json.dumps({"parent": parent["source_hash"],
        "manifests": manifests, "state": current["state_json"]},
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return bound == current["source_hash"] and context_retention.render_state(state) == current["summary"]


async def audit_user(session, plan):
    from app.services import checkpoint, checkpoint_repo, context_retention, contract_repo
    uid, generation = plan.user["user_id"], plan.user["memory_generation"]
    if any(row["canonical_text"] is None for row in plan.memories):
        return False
    if not plan.through:
        return True
    await context_retention.load_required_facts(session, user_id=uid, generation=generation)
    value = await checkpoint_repo.load_latest(session, uid, retention_enabled=True)
    coverage = await checkpoint_repo.coverage_for_checkpoint(session, user_id=uid,
        generation=generation, value=value)
    if coverage is None or coverage.through_message_id < plan.through:
        return False
    from app.core.time_utils import activity_date_for
    from datetime import datetime, timezone
    await contract_repo.published_text(session, user_id=uid, generation=generation,
        retention_enabled=True, locale=plan.user["language"],
        activity_date=activity_date_for(datetime.now(timezone.utc), plan.user["timezone"]))
    # A numeric cursor and an empty legacy document do not prove that the new
    # compiler read every normal source. Follow only successful fixed-target
    # progress receipts; never change/replay their history.
    receipts = (await session.execute(text("""SELECT payload,result_detail FROM async_jobs
      WHERE user_id=:uid AND job_type='contract_compile' AND state='succeeded'
        AND result_code='ok' AND payload->'retention_enabled'='true'::jsonb
        AND payload->'memory_generation'=to_jsonb(CAST(:generation AS bigint))
        AND payload->'privacy_epoch'=to_jsonb(CAST(:epoch AS bigint))
        AND payload->'through_message_id'=to_jsonb(CAST(:through AS bigint))"""),
        {"uid": uid, "generation": generation, "epoch": plan.user["barrier_epoch"],
         "through": plan.through})).all()
    normal_ids = {m["id"] for m in plan.messages
                  if m["kind"] == "normal" and m["id"] <= plan.through}
    reached = {0}
    for _ in range(len(receipts)+1):
        before = len(reached)
        for payload, detail in receipts:
            if isinstance(payload, dict) and isinstance(detail, dict):
                start, end = payload.get("after_message_id", 0), detail.get("through")
                if (type(start) is int and type(end) is int and start in reached
                        and end in normal_ids and end > start):
                    reached.add(end)
        if len(reached) == before:
            break
    normal_last = max(normal_ids, default=0)
    if max(reached) < normal_last:
        return False
    rows = (await session.execute(text("""SELECT * FROM conversation_checkpoints WHERE user_id=:uid
        AND memory_generation=:generation AND version=:version AND publish_state='published'"""),
        {"uid": uid, "generation": generation, "version": checkpoint.RETENTION_SUMMARIZER_VERSION})).mappings().all()
    chain = {r["id"]: r for r in rows}
    seen, current = set(), chain.get(value.id)
    while current is not None:
        if current["id"] in seen:
            return False
        seen.add(current["id"])
        parent_id = current["previous_checkpoint_id"]
        parent = chain.get(parent_id) if parent_id else None
        if parent_id and parent is None:
            return False
        validation = current["validation_json"] or {}
        if validation.get("processing_complete") is not True:
            return False
        if validation.get("reverified") is True:
            if not reverified_matches(current, parent, plan.messages):
                return False
            await context_retention.validate_progress_sources(session, user_id=uid,
                                                             state=current["state_json"])
            current = parent
            continue
        after = parent["through_message_id"] if parent else 0
        segment = [m for m in plan.messages if after < m["id"] <= current["through_message_id"]]
        if (not segment or segment[-1]["id"] != current["through_message_id"]
                or current["coverage_from_message_id"] != segment[0]["id"]
                or current["coverage_through_message_id"] != current["through_message_id"]
                or current["segment_through_message_id"] != current["through_message_id"]
                or (current["validation_json"] or {}).get("processing_complete") is not True
                or (current["validation_json"] or {}).get("source_message_ids") != [m["id"] for m in segment]):
            return False
        previous = checkpoint.Checkpoint(parent["id"], parent["through_message_id"], parent["summary"],
            parent["version"], parent["source_hash"], parent["state_json"], parent["validation_json"]) if parent else None
        sources = [checkpoint.SourceMessage(m["id"], m["sender"], m["kind"], m["content"] or "", m["created_at"]) for m in segment]
        if checkpoint.source_hash(previous=previous, messages=sources,
                                  version=current["version"]) != current["source_hash"]:
            return False
        proof = current["validation_json"].get("completed_proof")
        if not isinstance(proof, dict):
            return False
        options = {}
        if proof.get("packing_version") is not None:
            page = proof.get("page_proof")
            options = {"packing_version": proof["packing_version"],
                "packing_chunk_cap": proof.get("packing_chunk_cap"), "page_proof": page,
                "previous_state": page.get("before_state") if isinstance(page, dict) else None,
                "start_chunk": page.get("start_chunk", 0) if isinstance(page, dict) else 0}
        from app.models.message import SUMMARY_EXCLUDED_MESSAGE_KINDS
        rendered = checkpoint.validate_completed_progress(previous=previous,
            messages=[m for m in sources if m.kind not in SUMMARY_EXCLUDED_MESSAGE_KINDS],
            language=plan.user["language"], state=current["state_json"], proof=proof, **options)
        if rendered != current["summary"]:
            return False
        await context_retention.validate_progress_sources(session, user_id=uid, state=current["state_json"])
        current = parent
    return bool(seen)


async def run(args):
    from db.envfile import load_conn
    env_file = getattr(args, "env_file", None)
    if env_file is not None:
        env_file = Path(env_file)
        if (not env_file.is_absolute() or env_file.is_symlink()
                or not env_file.is_file() or env_file.stat().st_mode & 0o077
                or any(p.is_symlink() or (p / ".git").exists() for p in env_file.parents)):
            raise Blocked("private_environment_file_required")
    dsn = load_conn(str(env_file) if env_file else args.env)
    require_target(args.env, dsn, args.expected_project_ref, args.production_ack)
    if args.action == "prepare" and (args.maintenance_ack != MAINTENANCE_ACK
            or not args.plan or not args.plan_sha256):
        raise Blocked("maintenance_and_approved_plan_required")
    # Reserve the review artifact before any connection or possible commit.
    with reserve_private(args.output) as output:
        return await _run(args, dsn, output)


async def _run(args, dsn, output):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    url = re.sub(r"^postgres(?:ql)?(?:\+asyncpg)?://", "postgresql+asyncpg://", dsn)
    engine = create_async_engine(url, echo=False, hide_parameters=True, pool_size=1,
        max_overflow=0, connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0})
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            plans, blockers, count, excluded = await collect(session)
            current = artifact(plans, blockers, count, project_ref=args.expected_project_ref,
                               excluded=excluded)
            write_private(output, current)
            if args.action in {"prepare", "audit"}:
                from app.services import context_retention
                if not await context_retention.enabled_now(session):
                    raise Blocked("global_worker_flag_required")
            if args.action == "prepare":
                plan = json.loads(args.plan.read_text())
                approved_plan(plan, current, args.plan_sha256)
                if await session.scalar(_BUSY):
                    raise Blocked("maintenance_has_live_work")
            if args.action == "audit":
                if not args.plan or not args.plan_sha256:
                    raise Blocked("audit_requires_fixed_plan")
                approved_plan(json.loads(args.plan.read_text()), current, args.plan_sha256)
                ready = 0
                for item in plans:
                    try:
                        ready += bool(await audit_user(session, item))
                    except ValueError:
                        pass
                current["verified_users"] = ready
                current["ready"] = not blockers and ready == current["eligible_users"]
            await session.rollback()
        if args.action == "prepare":
            current["completed_users"] = 0
            for item in plans:
                if not item.through and not item.memories:
                    continue
                async with maker() as session:
                    try:
                        await prepare_user(session, item)
                        await session.commit()
                    except BaseException:
                        await session.rollback()
                        raise
                current["completed_users"] += 1
                current["missing_canonical"] -= sum(r["canonical_text"] is None for r in item.memories)
                write_private(output, current)
            current["reserved_all_users"] = True
        write_private(output, current)
        print(json.dumps({"action": args.action, "population": count,
                          "blockers": current["blockers"], "ready": current.get("ready", False),
                          "plan_sha256": digest(current)}, sort_keys=True))
        return 0 if not blockers and (args.action != "audit" or current["ready"]) else 1
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("plan", "prepare", "audit"), default="plan")
    parser.add_argument("--env", choices=("dev", "prod"), default="dev")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--expected-project-ref", required=True)
    parser.add_argument("--production-ack", action="store_true")
    parser.add_argument("--maintenance-ack")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from db.envfile import bootstrap_script_environment
    bootstrap_script_environment(["--env", str(args.env_file) if args.env_file else args.env])
    try:
        return asyncio.run(run(args))
    except Exception as exc:
        print(json.dumps({"action": args.action, "blocked": str(exc) if isinstance(exc, Blocked)
                          else type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
