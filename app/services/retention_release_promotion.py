"""Private, model-free preparation audit and atomic first global promotion.

Only ready preparation rows are promoted. Existing public contracts/checkpoints,
raw messages and fact-pipeline cursors are never rewritten by this module.
"""
from __future__ import annotations

from app.services import message_content

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import time
import uuid

from sqlalchemy import text

FORMAT = "guard-context-live-release-v2"
REQUIRED_COLUMNS = {
    ("mem0_memory_registry", "canonical_text"), ("mem0_memory_registry", "context_priority"),
    ("mem0_ingest_candidates", "context_priority"),
    ("chat_contexts", "contract_generation"), ("chat_contexts", "contract_processed_through_message_id"),
    ("conversation_checkpoints", "state_json"), ("conversation_checkpoints", "validation_json"),
    ("conversation_checkpoints", "preparation_id"), ("retention_preparations", "manifest_json"),
    ("retention_prepared_contracts", "processed_through_message_id"),
    ("retention_prepared_contracts", "validation_json"),
    ("retention_preparation_revisions", "revision"),
    ("retention_preparation_revisions", "through_message_id"),
    ("retention_preparation_revision_events", "id"),
}

class Blocked(ValueError):
    """A fixed safe reason; never include body, identifiers or driver messages."""


class AuditRefreshRequired(Blocked):
    def __init__(self, subjects):
        self.subjects = tuple(sorted({uuid.UUID(str(subject)) for subject in subjects}, key=str))
        if not self.subjects:
            raise Blocked("snapshot_metadata_unavailable")
        super().__init__("audit_refresh_required")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), default=str).encode()).hexdigest()


_USERS = text("""
SELECT p.id AS user_id,p.language,p.timezone,cc.memory_generation,cc.last_committed_turn_seq,
       p.xmin::text AS profile_version,
       COALESCE(b.state,'missing') AS barrier_state,COALESCE(b.epoch,0) AS barrier_epoch,
       s.mode,s.bootstrap_status,s.privacy_epoch,s.repair_generation
FROM profiles p LEFT JOIN chat_contexts cc ON cc.user_id=p.id
LEFT JOIN privacy_subject_barriers b ON b.user_id=p.id
LEFT JOIN memory_pipeline_states s ON s.user_id=p.id ORDER BY p.id
""")
_MESSAGES = """SELECT id,sender,kind,content,created_at,turn_seq,turn_position,xmin::text AS row_version,content_ciphertext,content_chars
FROM messages WHERE user_id=:uid ORDER BY id"""
_MEMORIES = """
SELECT r.id,r.provider,r.collection_version,r.provider_memory_id,r.content_hash,
 r.canonical_text,r.xmin::text AS row_version,
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
SELECT s.registry_id,s.source_message_id,s.source_sender,s.authority,s.xmin::text AS row_version,
 s.evidence_start_utf8 AS start_utf8,s.evidence_end_utf8 AS end_utf8,
 s.source_content_hash AS span_hash,s.source_occurred_at,
 m.id AS actual_message_id,m.sender AS message_sender,m.kind AS message_kind,
 m.content AS message_content,m.content_ciphertext AS message_ciphertext,m.content_chars AS message_chars
FROM mem0_memory_sources s LEFT JOIN messages m
 ON m.id=s.source_message_id AND m.user_id=s.user_id
WHERE s.user_id=:uid AND s.registry_id=ANY(CAST(:ids AS uuid[]))
ORDER BY s.registry_id,s.source_message_id,s.evidence_start_utf8,s.evidence_end_utf8
"""
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


async def read_user(session, user, *, lock=False, through_id=None):
    from app.services.provided_memory import _source
    if not authorize_metadata(user):
        raise Blocked("subject_not_authorized")
    uid = user["user_id"]
    message_sql = _MESSAGES
    memory_sql = _MEMORIES
    source_sql = _SOURCES
    parameters = {"uid": uid}
    if through_id is not None:
        if type(through_id) is not int or through_id < 0:
            raise Blocked("invalid_prefix_boundary")
        parameters["through"] = through_id
        message_sql = message_sql.replace("ORDER BY id", "AND id<=:through ORDER BY id")
        memory_sql = memory_sql.replace("ORDER BY r.id", "AND EXISTS(SELECT 1 FROM mem0_memory_sources x "
            "WHERE x.registry_id=r.id AND x.user_id=r.user_id AND x.source_message_id<=:through) ORDER BY r.id")
        source_sql = source_sql.replace("ORDER BY s.registry_id",
            "AND s.source_message_id<=:through ORDER BY s.registry_id")
    messages = [_logical_message(r, user_id=uid) for r in (await session.execute(
        text(message_sql + (" FOR SHARE" if lock else "")), parameters)).mappings().all()]
    memories = [dict(r) for r in (await session.execute(text(
        memory_sql + (" FOR UPDATE OF r" if lock else "")), parameters)).mappings().all()]
    ids = [r["id"] for r in memories]
    if lock and ids:
        await session.execute(text("""SELECT registry_id FROM mem0_memory_sources
          WHERE user_id=:uid AND registry_id=ANY(CAST(:ids AS uuid[]))
          ORDER BY registry_id,source_message_id,evidence_start_utf8,evidence_end_utf8 FOR SHARE"""),
          {"uid": uid, "ids": ids})
    sources = (await session.execute(text(source_sql), {**parameters, "ids": ids})).mappings().all() if ids else []
    evidence = {identity: [] for identity in ids}
    for source in sources:
        proof = _source(source, user_id=uid)
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
        "messages": [m for m in messages if m["id"] <= through], "through": through,
        "memories": [[str(r["id"]), r["provider"], r["collection_version"],
                      str(r["provider_memory_id"]), r["content_hash"], digest(canonical[r["id"]]),
                      sorted(evidence[r["id"]])] for r in memories]})
    return UserPlan(user, messages, memories, canonical, through, fingerprint)


async def schema_ready(session):
    rows = (await session.execute(text("""SELECT table_name,column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name IN
        ('mem0_memory_registry','mem0_ingest_candidates','chat_contexts','conversation_checkpoints',
         'retention_preparations','retention_prepared_contracts','retention_preparation_revisions',
         'retention_preparation_revision_events')"""))).all()
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


async def prepare_user(session, expected, *, preparation_id):
    from app.core.advisory_lock import advisory_xact_lock
    from app.services import checkpoint_repo, context_retention, retention_release
    from app.services.retention_preparation_context import Binding, current_binding
    if current_binding() != Binding(preparation_id, True):
        raise Blocked("building_binding_required")
    await retention_release.require_binding(session, lock=True)
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
    current = await read_user(session, user, lock=True, through_id=expected.through)
    if prefix_record(current) != prefix_record(expected):
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
        if scoped_source_hash(previous=None, messages=sources,
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
    # A cursor alone cannot prove that an empty contract processed the raw input.
    # Immutable per-segment proof survives ordinary async-job payload expiry.
    from app.services import retention_release
    from app.services.retention_preparation_context import current_binding
    binding = current_binding()
    normal_last = max((m["id"] for m in plan.messages
                       if m["kind"] == "normal" and m["id"] <= plan.through), default=0)
    if not await retention_release.validate_contract_chain(session, user_id=uid,
            generation=generation, through_id=normal_last):
        return False
    rows = (await session.execute(text("""SELECT * FROM conversation_checkpoints WHERE user_id=:uid
        AND memory_generation=:generation AND version=:version AND publish_state=:publication_state
        AND preparation_id IS NOT DISTINCT FROM CAST(:preparation_id AS uuid)"""),
        {"uid": uid, "generation": generation, "version": checkpoint.RETENTION_SUMMARIZER_VERSION,
         "publication_state": "ready" if binding and binding.building else "published",
         "preparation_id": binding.preparation_id if binding else None})).mappings().all()
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
        if scoped_source_hash(previous=previous, messages=sources,
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


def scoped_source_hash(**kwargs):
    from app.services import checkpoint
    from app.services.retention_preparation_context import current_binding
    value = current_binding()
    return checkpoint.source_hash(**kwargs, preparation_id=value.preparation_id if value else None)


def prefix_record(plan):
    """Only immutable source input; public derived-memory maintenance stays live.

    Current active canonical/source evidence is independently certified on every
    read and again by audit/final row proof. It is never an old draft's identity.
    """
    fields = ("id", "sender", "kind", "content", "created_at", "turn_seq", "turn_position")
    generation = plan.user["memory_generation"]
    # A genuinely empty account may acquire its initial generation-0 context
    # while preparation is live. This is initialization, never a forgotten-era reset.
    if generation is None and plan.through == 0:
        generation = 0
    source = {"owner": str(plan.user["user_id"]),
              "generation": generation, "epoch": plan.user["barrier_epoch"],
              "language": plan.user.get("language"), "timezone": plan.user.get("timezone"),
              "through": plan.through,
              "messages": [[m.get(key) for key in fields] for m in plan.messages if m["id"] <= plan.through]}
    return {"through": plan.through, "fingerprint": digest(source),
            "generation": generation, "epoch": plan.user["barrier_epoch"]}


def valid_sha(value):
    import re
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def live_manifest(plans, excluded, *, project_ref, code_sha):
    if not valid_sha(code_sha):
        raise Blocked("expected_code_sha_required")
    return {"format": FORMAT, "project_ref": project_ref, "code_sha": code_sha,
            "users": {str(p.user["user_id"]): prefix_record(p) for p in plans},
            "excluded": excluded}


def public_report(manifest, *, preparation_id=None, ready=False, blockers=None):
    """Internal manifest contains identities; this is the only serializable report."""
    return {"format": FORMAT, "project_ref": manifest["project_ref"],
            "code_sha": manifest["code_sha"], "preparation_id": str(preparation_id) if preparation_id else None,
            "eligible_users": len(manifest["users"]),
            "history_users": sum(v["through"] > 0 for v in manifest["users"].values()),
            "excluded_authorization": dict(Counter(v[1] for v in manifest["excluded"])),
            "source_fingerprint": digest(manifest), "ready": bool(ready),
            "blockers": dict(blockers or {})}


async def registry(session, identity, *, lock=False):
    statement = "SELECT id,state,code_sha,manifest_json,xmin::text AS row_version FROM retention_preparations WHERE id=:id"
    row = (await session.execute(text(statement + (" FOR UPDATE NOWAIT" if lock else "")),
                                 {"id": identity})).mappings().first()
    if row is None or row["manifest_json"].get("format") != FORMAT:
        raise Blocked("live_preparation_required")
    if row["code_sha"] != row["manifest_json"].get("code_sha"):
        raise Blocked("preparation_code_binding_changed")
    return dict(row)


async def enroll_source_revisions(session, identity, manifest):
    """Drain old-boundary writers before enrolling or extending tracked inputs."""
    await fence_revision_registration(session)
    await _enroll_source_revisions_fenced(session,identity,manifest)


async def _enroll_source_revisions_fenced(session, identity, manifest):
    """The caller owns the registration sentinel through commit."""
    targets = [{"user_id": key, "through": record["through"]}
               for key, record in manifest["users"].items()]
    await session.execute(text("SELECT user_id FROM retention_preparation_revisions "
        "WHERE preparation_id=:id AND user_id=ANY(CAST(:ids AS uuid[])) "
        "ORDER BY user_id FOR UPDATE NOWAIT"),
        {"id": identity, "ids": [uuid.UUID(item["user_id"]) for item in targets]})
    await session.execute(text("""INSERT INTO retention_preparation_revisions
      (preparation_id,user_id,through_message_id,revision)
      SELECT :id,t.user_id,t.through,0 FROM jsonb_to_recordset(CAST(:targets AS jsonb))
        AS t(user_id uuid,through bigint)
      ON CONFLICT(preparation_id,user_id) DO UPDATE
        SET through_message_id=EXCLUDED.through_message_id,
            revision=retention_preparation_revisions.revision+1
      WHERE retention_preparation_revisions.through_message_id IS DISTINCT FROM EXCLUDED.through_message_id
      """), {"id": identity, "targets": json.dumps(targets)})


async def audited_source_revisions(session, identity, plans):
    boundaries = {str(plan.user["user_id"]): plan.through for plan in plans}
    rows = (await session.execute(text("""WITH event_counts AS MATERIALIZED (
        SELECT user_id,count(*) AS total FROM retention_preparation_revision_events
        WHERE preparation_id=:id GROUP BY user_id)
        SELECT r.user_id,r.through_message_id,r.revision+COALESCE(e.total,0) AS revision
        FROM retention_preparation_revisions r LEFT JOIN event_counts e ON e.user_id=r.user_id
        WHERE r.preparation_id=:id AND r.user_id=ANY(CAST(:ids AS uuid[])) ORDER BY r.user_id"""),
        {"id": identity, "ids": [uuid.UUID(key) for key in boundaries]})).mappings().all()
    if (len(rows) != len(boundaries) or {str(row["user_id"]) for row in rows} != set(boundaries)
            or any(row["through_message_id"] != boundaries[str(row["user_id"])]
                   or type(row["revision"]) is not int or row["revision"] < 0 for row in rows)):
        raise Blocked("preparation_revision_unregistered")
    return {str(row["user_id"]): row["revision"] for row in rows}


async def fence_revision_registration(session):
    value = await session.scalar(text("SELECT value FROM app_config "
        "WHERE key='context_retention_revision_fence' FOR UPDATE NOWAIT"))
    if type(value) is not int or value < 0:
        raise Blocked("revision_registration_fence_unavailable")
    await session.execute(text("UPDATE app_config SET value=to_jsonb(CAST(:next_value AS bigint)),"
        "updated_at=now() WHERE key='context_retention_revision_fence'"), {"next_value":value+1})


async def new_preparation(session, *, plans, excluded, project_ref, code_sha):
    """One building release. Old aborted history and terminal failures stay intact."""
    # Serialize start/activate/abort without closing any public API route.
    await session.execute(text("SELECT pg_advisory_xact_lock(743021149)"))
    existing = await session.scalar(text("SELECT EXISTS(SELECT 1 FROM retention_preparations "
        "WHERE state IN ('building','prepared','active'))"))
    if existing:
        raise Blocked("existing_preparation_requires_explicit_resolution")
    manifest = live_manifest(plans, excluded, project_ref=project_ref, code_sha=code_sha)
    # A new UUID is never a retry-budget reset for an unchanged failed subject.
    archived = (await session.execute(text("SELECT manifest_json FROM retention_preparations WHERE state='aborted'"))).scalars().all()
    for old in archived:
        for key, record in old.get("terminal_failures", {}).items():
            if key in manifest["users"] and manifest["users"][key] == record:
                raise Blocked("unchanged_terminal_failure_requires_manual_resolution")
    failed = (await session.execute(text("""SELECT DISTINCT p.manifest_json,j.user_id
      FROM retention_preparations p JOIN async_jobs j
        ON (j.payload->>'retention_preparation_id'=p.id::text
            OR j.dedup_key LIKE concat('%',chr(58),'preparation',chr(58),p.id::text,chr(58),'1'))
      WHERE p.state='aborted' AND j.state='dead'
        AND j.job_type IN ('conversation_checkpoint','contract_compile')"""))).all()
    for old, user_id in failed:
        key = str(user_id)
        if old.get("users", {}).get(key) == manifest["users"].get(key) and key in manifest["users"]:
            raise Blocked("unchanged_terminal_failure_requires_manual_resolution")
    await fence_revision_registration(session)
    identity = uuid.uuid4()
    await session.execute(text("""INSERT INTO retention_preparations(id,state,code_sha,manifest_json)
       VALUES(:id,'building',:sha,CAST(:manifest AS jsonb))"""),
       {"id": identity, "sha": code_sha, "manifest": json.dumps(manifest, default=str)})
    await _enroll_source_revisions_fenced(session, identity, manifest)
    return identity, manifest


async def prepare_all(maker, identity, *, catch_up=False):
    """Bounded per-user commits. Partial runs can be repeated with the same ID."""
    from app.services.retention_preparation_context import Binding, use_binding
    with use_binding(Binding(identity, True)):
        async with maker() as session:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            row = await registry(session, identity)
            if row["state"] not in {"building", "prepared"}:
                raise Blocked("preparation_not_building")
            original = row["manifest_json"]
            users = [dict(r) for r in (await session.execute(_USERS)).mappings().all()]
            by_id = {str(u["user_id"]): u for u in users}
            # Before changing a target, validate the original fixed prefix.
            for key, record in original["users"].items():
                user = by_id.get(key)
                if user is None:
                    raise Blocked("prefix_owner_changed")
                prior = await read_user(session, user, through_id=record["through"])
                if prefix_record(prior) != record:
                    raise Blocked("prefix_changed_abort_and_replan")
            plans, blockers, _, excluded = await collect(session)
            if blockers:
                raise Blocked("current_source_blocked")
            manifest = live_manifest(plans, excluded, project_ref=original["project_ref"],
                                     code_sha=original["code_sha"])
            if not catch_up:
                # Resume the old target, never silently advance a partially built run.
                plans = [await read_user(session, by_id[key], through_id=record["through"])
                         for key, record in original["users"].items()]
                manifest = original
            await session.rollback()
        if catch_up:
            async with maker() as session:
                locked = await registry(session, identity, lock=True)
                if locked["state"] not in {"building", "prepared"} or locked["manifest_json"] != original:
                    raise Blocked("concurrent_preparation_changed")
                await session.execute(text("UPDATE retention_preparations SET state='building', "
                    "manifest_json=CAST(:manifest AS jsonb),updated_at=now() WHERE id=:id"),
                    {"id": identity, "manifest": json.dumps(manifest, default=str)})
                await enroll_source_revisions(session, identity, manifest)
                await session.commit()
        for plan in plans:
            async with maker() as session:
                try:
                    await prepare_user(session, plan, preparation_id=identity)
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    raise
        return public_report(manifest, preparation_id=identity)


@dataclass(frozen=True)
class AuditToken:
    preparation_id: uuid.UUID
    manifest_hash: str
    code_sha: str
    created_monotonic: float
    user_metadata: tuple
    row_versions: dict
    tail_contexts: dict
    report: dict
    manifest: dict
    registry_version: str
    source_revisions: dict


def metadata_record(user):
    return (str(user["user_id"]), user["language"], user["timezone"], user["memory_generation"],
            user["barrier_state"], user["barrier_epoch"], user["mode"], user["bootstrap_status"],
            user["privacy_epoch"], user["repair_generation"])


async def tail_context(session, plan):
    """Actual first-hop wire preview; no model, vector, tool or embedding execution."""
    from app.core.time_utils import activity_date_for
    from app.services import checkpoint_repo, context_retention, contract_repo
    from app.services.chat import _build_system
    uid, generation = plan.user["user_id"], plan.user["memory_generation"]
    value = await checkpoint_repo.load_latest(session, uid, retention_enabled=True) if plan.through else None
    facts = await context_retention.load_required_facts(session, user_id=uid, generation=generation or 0)
    contract = await contract_repo.published_text(session, user_id=uid, generation=generation or 0,
        retention_enabled=True, locale=plan.user["language"],
        activity_date=activity_date_for(datetime.now(timezone.utc), plan.user["timezone"])) if plan.through else ""
    system = _build_system(plan.user["language"], None, summary=value.summary if value else "",
                           crisis_guard_enabled=True)
    return {"system": system, "facts": facts, "contract": contract,
            "through": value.through_message_id if value else 0}


def check_tail_budget(context, messages):
    from app.services import context_retention
    from app.services.agent.runtime import foreground_budget_inputs
    convo = [{"role": "system", "content": "\n\n".join(v for v in
              (context["facts"], context["contract"]) if v)}]
    convo.extend({"role": "user" if m["sender"] == "user" else "assistant", "content": m["content"] or ""}
        for m in messages if m["id"] > context["through"] and m["kind"] == "normal")
    # Readiness covers the known current tail with explicit request/context room.
    # It cannot guarantee that an arbitrarily large future user input will fit.
    convo.append({"role": "user", "content": ""})
    system, wire, tools = foreground_budget_inputs(context["system"], convo, user_text="")
    context_retention.assert_input_budget(system, wire, tools=tools,
        input_limit=context_retention.FOREGROUND_INPUT_LIMIT, output_reserve=4096)


def row_versions_statement(identity, plans, *, lock=False):
    """Build the unchanged nine-scope source proof without performing I/O."""
    ids = [p.user["user_id"] for p in plans]
    boundaries = {str(p.user["user_id"]): p.through for p in plans}
    params = {"ids": ids, "id": identity, "bounds": json.dumps(boundaries)}
    queries = {
        "profiles": "SELECT p.id,p.language,p.timezone FROM profiles p WHERE p.id=ANY(CAST(:ids AS uuid[])) ORDER BY p.id",
        "pipelines": "SELECT p.user_id AS id,p.mode,p.bootstrap_status,p.privacy_epoch,p.repair_generation FROM memory_pipeline_states p WHERE p.user_id=ANY(CAST(:ids AS uuid[])) ORDER BY p.user_id",
        "contexts": "SELECT c.user_id AS id,c.memory_generation AS value FROM chat_contexts c WHERE c.user_id=ANY(CAST(:ids AS uuid[])) ORDER BY c.user_id",
        "barriers": "SELECT b.user_id AS id,b.state,b.epoch FROM privacy_subject_barriers b WHERE b.user_id=ANY(CAST(:ids AS uuid[])) ORDER BY b.user_id",
        "messages": "SELECT m.id,m.xmin::text AS version FROM messages m WHERE m.user_id=ANY(CAST(:ids AS uuid[])) AND (m.id<=COALESCE((CAST(:bounds AS jsonb)->>m.user_id::text)::bigint,0) OR EXISTS(SELECT 1 FROM mem0_memory_sources s JOIN mem0_memory_registry r ON r.id=s.registry_id WHERE s.user_id=m.user_id AND s.source_message_id=m.id AND r.semantic_status='active' AND r.provider_delete_state='kept')) ORDER BY m.id",
        "memories": "SELECT r.id,r.user_id AS owner_id,r.xmin::text AS version FROM mem0_memory_registry r WHERE r.user_id=ANY(CAST(:ids AS uuid[])) AND r.semantic_status='active' AND r.provider_delete_state='kept' ORDER BY r.id",
        "sources": "SELECT s.registry_id,s.source_message_id,s.evidence_start_utf8,s.evidence_end_utf8,s.xmin::text AS version FROM mem0_memory_sources s JOIN locked_5 r ON r.id=s.registry_id AND r.owner_id=s.user_id WHERE s.user_id=ANY(CAST(:ids AS uuid[])) ORDER BY s.registry_id,s.source_message_id,s.evidence_start_utf8,s.evidence_end_utf8",
        "checkpoints": "SELECT c.id,c.xmin::text AS version FROM conversation_checkpoints c WHERE c.preparation_id=:id AND c.publish_state='ready' ORDER BY c.id",
        "contracts": "SELECT c.id,c.xmin::text AS version FROM retention_prepared_contracts c WHERE c.preparation_id=:id AND c.status IN ('ready','superseded') ORDER BY c.id",
    }
    definitions, projections = [], []
    for index, (name, sql) in enumerate(queries.items()):
        if name == "sources" and not lock:
            # Keep the join hashable even when freshly prepared data has no
            # useful statistics. Materialization prevents the outer predicate
            # reducing FULL to a per-source registry/index nested lookup.
            definitions.append("joined_sources AS MATERIALIZED ("
                "SELECT s.registry_id,s.user_id,s.source_message_id,s.evidence_start_utf8,"
                "s.evidence_end_utf8,s.xmin::text AS version,r.owner_id AS matching_owner "
                "FROM mem0_memory_sources s FULL JOIN locked_5 r "
                "ON r.id=s.registry_id AND r.owner_id=s.user_id)")
            sql = ("SELECT registry_id,source_message_id,evidence_start_utf8,evidence_end_utf8,version "
                "FROM joined_sources WHERE matching_owner IS NOT NULL AND registry_id IS NOT NULL "
                "AND user_id=ANY(CAST(:ids AS uuid[])) "
                "ORDER BY registry_id,source_message_id,evidence_start_utf8,evidence_end_utf8")
        # Fixed SQL identifiers only; actual source rows are still locked. Return
        # nine tiny signatures in one round trip, not the whole source ID vector.
        suffix = " FOR UPDATE OF s NOWAIT" if name == "sources" else " FOR UPDATE NOWAIT"
        inner = sql + (suffix if lock else "")
        alias = f"locked_{index}"
        definitions.append(f"{alias} AS MATERIALIZED ({inner})")
        keys = ("registry_id", "source_message_id", "evidence_start_utf8", "evidence_end_utf8") if name == "sources" else ("id",)
        order = ",".join(f"{alias}.{key}" for key in keys)
        if name in {"messages", "memories", "checkpoints", "contracts", "sources"}:
            # These fields are NOT NULL database integers/UUIDs/xmin decimals:
            # neither '|' nor newline can occur, so this fixed-arity encoding
            # preserves every proof value injectively without JSON key overhead.
            fields = (*keys, "version")
            values = ",".join(f"{alias}.{key}::text" for key in fields)
            serialized = f"concat_ws('|',{values})"
        else:
            # Nullable/text metadata retains its explicit JSON null semantics.
            serialized = f"row_to_json({alias})::text"
        projections.append(f"SELECT '{name}' AS scope,count(*) AS count,"
            f"encode(sha256(convert_to(COALESCE(string_agg({serialized}, E'\\n' "
            f"ORDER BY {order}),''),'UTF8')),'hex') AS signature FROM {alias}")
    statement = "WITH " + ",".join(definitions) + " " + " UNION ALL ".join(projections)
    return statement, params


def _row_versions_result(rows):
    result = {r["scope"]: {"count": r["count"], "signature": r["signature"]} for r in rows}
    if len(rows) != 9 or set(result) != {"profiles", "pipelines", "contexts", "barriers",
            "messages", "memories", "sources", "checkpoints", "contracts"}:
        raise Blocked("snapshot_metadata_unavailable")
    return result


async def row_versions(session, identity, plans, *, lock=False):
    """Exact rows audited, then held through promotion; existing source edits reject."""
    statement, params = row_versions_statement(identity, plans, lock=lock)
    return _row_versions_result((await session.execute(text(statement), params)).mappings().all())


async def activation_snapshot(session, identity, plans, manifest):
    """One statement binds all nine signatures and the identical completion proof."""
    signatures, signature_params = row_versions_statement(identity, plans)
    completion, completion_params = processing_statement(identity, manifest)
    statement = ("WITH signatures AS MATERIALIZED (" + signatures + "), "
        "completion(value) AS MATERIALIZED (" + completion + ") "
        "SELECT signatures.*,completion.value AS processing_complete "
        "FROM signatures CROSS JOIN completion")
    rows = (await session.execute(text(statement),
        {**signature_params, **completion_params})).mappings().all()
    versions = _row_versions_result(rows)
    statuses = [row["processing_complete"] for row in rows]
    if any(type(value) is not bool for value in statuses) or len(set(statuses)) != 1:
        raise Blocked("snapshot_metadata_unavailable")
    return versions, statuses[0]


async def audit_preparation(session, identity):
    from app.services.retention_preparation_context import Binding, use_binding
    # This is the long, no-lock proof phase. A read-committed caller could bind
    # old source validation to a newer xmin token and must fail closed.
    isolation = await session.scalar(text("SELECT current_setting('transaction_isolation')"))
    if isolation not in {"repeatable read", "serializable"}:
        raise Blocked("consistent_audit_snapshot_required")
    if await session.scalar(text("SELECT current_setting('transaction_read_only')")) != "on":
        raise Blocked("readonly_audit_snapshot_required")
    row = await registry(session, identity)
    if row["state"] not in {"building", "prepared"}:
        raise Blocked("preparation_not_building")
    manifest = row["manifest_json"]
    plans, blockers, _, excluded = await collect(session)
    if blockers:
        raise Blocked("current_source_blocked")
    by_id = {str(p.user["user_id"]): p for p in plans}
    if any(memory["canonical_text"] is None for plan in plans for memory in plan.memories):
        raise Blocked("canonical_backfill_incomplete")
    verified, contexts = [], {}
    with use_binding(Binding(identity, True)):
        for key, record in manifest["users"].items():
            current = by_id.get(key)
            if current is None:
                raise Blocked("prefix_owner_changed")
            prefix = await read_user(session, current.user, through_id=record["through"])
            if prefix_record(prefix) != record:
                raise Blocked("prefix_changed_abort_and_replan")
            if not await audit_user(session, prefix):
                raise Blocked("prepared_sources_incomplete")
            contexts[key] = await tail_context(session, prefix)
            check_tail_budget(contexts[key], current.messages)
            contexts[key]["tail_hash"] = tail_digest([m for m in current.messages if m["id"] > contexts[key]["through"]])
            verified.append(prefix)
        for key, current in by_id.items():
            if key not in manifest["users"]:
                if current.through or current.memories:
                    raise Blocked("new_history_requires_catch_up")
                contexts[key] = await tail_context(session, current)
                check_tail_budget(contexts[key], current.messages)
                contexts[key]["tail_hash"] = tail_digest(current.messages)
                verified.append(current)
        # No eligible subject may silently disappear into an exclusion set.
        if any(str(x[0]) in manifest["users"] for x in excluded):
            raise Blocked("subject_authorization_changed")
        versions = await row_versions(session, identity, verified)
        revisions = await audited_source_revisions(session, identity, verified)
    report = public_report(manifest, preparation_id=identity, ready=True)
    return AuditToken(identity, digest(manifest), row["code_sha"], time.monotonic(),
        tuple(metadata_record(p.user) for p in verified), versions, contexts, report,
        manifest, row["row_version"], revisions)


async def refresh_audit_subjects(maker, token, subjects):
    """Recheck only changed subjects; preserve the original deadline/retry history."""
    from dataclasses import replace
    from app.services.retention_preparation_context import Binding, use_binding
    keys = {str(uuid.UUID(str(subject))) for subject in subjects}
    if not keys or not keys.issubset(token.tail_contexts):
        raise Blocked("preparation_revision_unregistered")
    if time.monotonic()-token.created_monotonic > 60:
        raise Blocked("audit_token_expired")
    async with maker() as session:
        row = await registry(session,token.preparation_id)
        if row["state"] not in {"building","prepared"} or digest(row["manifest_json"]) != token.manifest_hash:
            raise Blocked("preparation_changed_since_audit")
        await seal_preparation(session,token.preparation_id)
        await session.commit()
    contexts, revisions = dict(token.tail_contexts), dict(token.source_revisions)
    metadata = {record[0]:record for record in token.user_metadata}
    with use_binding(Binding(token.preparation_id,True)):
        for key in sorted(keys):
            async with maker() as session:
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                row = await registry(session,token.preparation_id)
                if (row["state"] not in {"building","prepared"}
                        or row["code_sha"] != token.code_sha
                        or row["row_version"] != token.registry_version
                        or digest(row["manifest_json"]) != token.manifest_hash):
                    raise Blocked("preparation_changed_since_audit")
                user = (await session.execute(text("SELECT u.* FROM ("+str(_USERS)+") u WHERE u.user_id=:uid"),
                    {"uid":uuid.UUID(key)})).mappings().first()
                if user is None:
                    raise Blocked("prefix_owner_changed")
                user = dict(user)
                if not authorize_metadata(user):
                    raise Blocked("subject_authorization_changed")
                record = token.manifest["users"].get(key)
                if record is None:
                    raise Blocked("preparation_revision_unregistered")
                prefix = await read_user(session,user,through_id=record["through"])
                if prefix_record(prefix) != record:
                    raise Blocked("prefix_changed_abort_and_replan")
                if not await audit_user(session,prefix):
                    raise Blocked("prepared_sources_incomplete")
                current = await read_user(session,user)
                context = await tail_context(session,prefix)
                check_tail_budget(context,current.messages)
                context["tail_hash"] = tail_digest([m for m in current.messages if m["id"]>context["through"]])
                contexts[key] = context
                revisions.update(await audited_source_revisions(session,token.preparation_id,[prefix]))
                metadata[key] = metadata_record(user)
                await session.rollback()
    return replace(token,tail_contexts=contexts,source_revisions=revisions,
                   user_metadata=tuple(metadata[key] for key in sorted(metadata)))


async def lock_audited_registry(session, token):
    # RR audit already read the complete manifest. A matching MVCC version
    # proves it is still that exact row without transferring it a second time.
    row = (await session.execute(text("""SELECT id,state,code_sha,
      manifest_json->>'format' AS format,manifest_json->>'code_sha' AS bound_code_sha,
      xmin::text AS row_version,
      (SELECT id FROM retention_preparations WHERE state='active') AS active_id
      FROM retention_preparations WHERE id=:id FOR UPDATE NOWAIT"""),
      {"id":token.preparation_id})).mappings().first()
    if (row is None or row["format"] != FORMAT or row["code_sha"] != token.code_sha
            or row["bound_code_sha"] != row["code_sha"]):
        raise Blocked("live_preparation_required")
    if row["state"] != "active" and row["row_version"] != token.registry_version:
        raise Blocked("preparation_changed_since_audit")
    return {**dict(row), "manifest_json":token.manifest}


async def seal_preparation(session, identity):
    """Mark complete private artifacts; caller commits before a fresh full audit."""
    state = await session.scalar(text("SELECT state FROM retention_preparations "
        "WHERE id=:id FOR SHARE"), {"id": identity})
    if state not in {"building", "prepared"}:
        raise Blocked("preparation_not_building")
    return await session.scalar(text("""
      WITH sealed AS (
        UPDATE conversation_checkpoints
        SET validation_json=validation_json || jsonb_build_object('activation_sealed',true)
        WHERE preparation_id=:id AND publish_state='ready'
          AND jsonb_typeof(state_json)='object'
          AND jsonb_typeof(validation_json)='object'
          AND validation_json->'processing_complete'='true'::jsonb
          AND (jsonb_typeof(validation_json->'completed_proof')='object'
            OR (validation_json->'reverified'='true'::jsonb
              AND jsonb_typeof(validation_json->'page_manifests')='array'
              AND validation_json->'page_manifests'<>'[]'::jsonb))
          AND validation_json->'activation_sealed' IS DISTINCT FROM 'true'::jsonb
        RETURNING id
      ) SELECT count(*) FROM sealed
      """), {"id": identity})


async def acquire_activation_lock(session):
    """Configure the local bound before a nonblocking, same-key xact lock."""
    acquired = await session.scalar(text("""
      WITH configured AS MATERIALIZED (
        SELECT set_config('lock_timeout','150ms',true),
               set_config('statement_timeout','1000ms',true),
               set_config('idle_in_transaction_session_timeout','1000ms',true),
               set_config('work_mem','16MB',true)
      )
      SELECT pg_try_advisory_xact_lock(743021149) FROM configured
      """))
    if acquired is not True:
        raise Blocked("activation_busy")


async def fence_activation_sources(session):
    # One short transaction fence replaces tens of thousands of tuple locks.
    # SHARE permits normal reads/FK checks but excludes every existing/new DML
    # writer. A busy table rejects immediately; never queue behind live chat.
    await session.execute(text("""LOCK TABLE public.profiles, public.memory_pipeline_states,
      public.chat_contexts, public.privacy_subject_barriers, public.messages,
      public.mem0_memory_registry, public.mem0_memory_sources,
      public.mem0_ingest_candidates, public.conversation_checkpoints,
      public.retention_prepared_contracts IN SHARE MODE NOWAIT"""))


async def activate(session, token, *, deadline, prepared):
    """Caller commits. Any failure MUST roll back this entire transaction."""
    if time.monotonic()-token.created_monotonic > 60 or deadline <= time.monotonic():
        raise Blocked("audit_token_expired")
    async with asyncio.timeout_at(min(deadline, time.monotonic()+1)):
        await acquire_activation_lock(session)
        from app.services.retention_activation_db import validation_envelope
        receipt = await validation_envelope(session, token, prepared)
        row = receipt["registry"]
        controls = {item["key"]:item["value"] for item in receipt["controls"]}
        if row["state"] == "active":
            if controls.get("context_retention_active_preparation") != str(token.preparation_id):
                raise Blocked("active_preparation_pointer_conflict")
            return token.report
        if (row["state"] not in {"building", "prepared"}
                or digest(token.manifest) != token.manifest_hash):
            raise Blocked("preparation_changed_since_audit")
        if "dirty_subjects" in receipt:
            raise AuditRefreshRequired(receipt["dirty_subjects"])
        if receipt.get("verified_revisions") is not True:
            raise Blocked("snapshot_metadata_unavailable")
        expected = {key:(token.manifest["users"].get(key, {}).get("through",0), revision)
                    for key, revision in token.source_revisions.items()}
        rows = receipt["revision_rows"]
        actual = {str(item["user_id"]):(item["through_message_id"],item["revision"]) for item in rows}
        if len(rows) != len(expected) or actual != expected:
            raise Blocked("snapshot_metadata_unavailable")
        # The envelope only reads already-authorized subjects' tail. New users
        # remain metadata until the same privacy policy explicitly permits them.
        new_ids = [uuid.UUID(str(user["user_id"])) for user in receipt["new_users"]
                   if authorize_metadata(user)]
        all_messages = [_logical_message(row, receipt=True) for row in receipt["tail"]]
        if new_ids:
            all_messages.extend(_logical_message(row) for row in (await session.execute(text(
                "SELECT user_id,id,sender,kind,content,content_ciphertext,content_chars FROM messages "
                "WHERE user_id=ANY(CAST(:ids AS uuid[])) ORDER BY id FOR SHARE NOWAIT"),
                {"ids":new_ids})).mappings().all())
        grouped = {}
        for message in all_messages:
            key = str(message["user_id"])
            if key not in token.tail_contexts:
                raise Blocked("new_history_requires_catch_up")
            grouped.setdefault(key, []).append(message)
        for key, context in token.tail_contexts.items():
            tail = grouped.get(key, [])
            if tail_digest(tail) != context["tail_hash"]:
                check_tail_budget(context, tail)
        values = {"context_retention_active_preparation": str(token.preparation_id),
                  "context_retention_v2_enabled": True, "crisis_guard_enabled": True,
                  "crisis_guard_policy_version": "crisis-v2", "crisis_guard_timeout_s": 2,
                  "crisis_guard_history_messages": 6}
        # Global visibility switches atomically; sealed private artifacts keep
        # their ready status. No per-artifact mass updates hold the critical fence.
        publication = (await session.execute(text("""
          WITH job_cancellation AS (
            UPDATE async_jobs SET state='cancelled',lease_owner=NULL,lease_token=NULL,lease_until=NULL,
              result_code='preparation_activated',finished_at=now(),
              payload_expires_at=COALESCE(payload_expires_at,now()+interval '24 hours')
            WHERE id IN (SELECT id FROM async_jobs WHERE state='ready'
              AND payload->>'retention_preparation_id'=:preparation_key
              AND payload->'retention_preparation_building'='true'::jsonb FOR UPDATE SKIP LOCKED)
            RETURNING id
          ), preparation_activation AS (
            UPDATE retention_preparations SET state='active',updated_at=now()
            WHERE id=:preparation_id AND state IN ('building','prepared')
              AND (SELECT count(*) FROM job_cancellation)>=0 RETURNING id
          ), control_publication AS (
            INSERT INTO app_config(key,value)
            SELECT e.key,e.value FROM jsonb_each(CAST(:values AS jsonb)) e
            CROSS JOIN preparation_activation WHERE true
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now() RETURNING key
          ) SELECT (SELECT count(*) FROM preparation_activation) AS preparations_activated,
                   (SELECT count(*) FROM control_publication) AS controls_written
          """), {"preparation_id": token.preparation_id,
                  "preparation_key": str(token.preparation_id), "values": json.dumps(values)})).mappings().first()
        if publication is None or publication["preparations_activated"] != 1 or publication["controls_written"] != len(values):
            raise Blocked("publication_state_changed")
    return token.report


async def abort(session, identity):
    await session.execute(text("SELECT pg_advisory_xact_lock(743021149)"))
    row = await registry(session, identity, lock=True)
    if row["state"] == "active":
        raise Blocked("active_preparation_cannot_be_aborted")
    dead_users = (await session.execute(text("SELECT DISTINCT user_id FROM async_jobs WHERE state='dead' "
        "AND (payload->>'retention_preparation_id'=:id OR dedup_key LIKE concat('%',chr(58),'preparation',chr(58),:id,chr(58),'1')) "
        "AND job_type IN ('conversation_checkpoint','contract_compile')"), {"id": str(identity)})).scalars().all()
    manifest = dict(row["manifest_json"])
    failures = dict(manifest.get("terminal_failures", {}))
    for uid in dead_users:
        key = str(uid)
        if key in manifest["users"]:
            failures[key] = manifest["users"][key]
    manifest["terminal_failures"] = failures
    await session.execute(text("UPDATE retention_preparations SET state='aborted',manifest_json=CAST(:manifest AS jsonb),"
        "updated_at=now() WHERE id=:id"), {"id": identity, "manifest": json.dumps(manifest)})
    await session.execute(text("UPDATE async_jobs SET state='cancelled',lease_owner=NULL,lease_token=NULL,lease_until=NULL,"
        "result_code='preparation_aborted',finished_at=now(),"
        "payload_expires_at=COALESCE(payload_expires_at,now()+interval '24 hours') WHERE id IN (SELECT id FROM async_jobs WHERE state='ready' "
        "AND payload->>'retention_preparation_id'=:id AND payload->'retention_preparation_building'='true'::jsonb "
        "FOR UPDATE SKIP LOCKED)"),
        {"id": str(identity)})
    return public_report(row["manifest_json"], preparation_id=identity)


def _logical_message(row, *, user_id=None, receipt=False):
    raw = dict(row._mapping if hasattr(row, "_mapping") else row)
    if receipt:
        if "content_ciphertext" in raw:
            raise message_content.ContentCryptoError("content_receipt_hex_invalid")
        encoded = raw.pop("content_ciphertext_hex", None)
        if encoded is not None:
            if (not isinstance(encoded, str) or not 64 <= len(encoded) <= 2 * (1_048_576 + 32)
                    or len(encoded) % 2 or re.fullmatch(r"[0-9a-f]+", encoded) is None):
                raise message_content.ContentCryptoError("content_receipt_hex_invalid")
            raw["content_ciphertext"] = bytes.fromhex(encoded)
    decoded = message_content.project_message(raw, user_id=user_id)
    decoded.pop("content_ciphertext", None)
    decoded.pop("content_chars", None)
    return decoded


def tail_digest(messages):
    return digest([[m["id"], m["sender"], m["kind"], m["content"]] for m in messages])


def processing_statement(identity, manifest):
    rows = [{"user_id": key, **{field: record[field] for field in ("through", "generation", "epoch")}}
            for key, record in manifest["users"].items() if record["through"]]
    # Keep the exact single-user completion predicates. Materialized FULL joins
    # make each metadata set keyed and linear even when a fresh JSON manifest or
    # newly inserted rows are severely underestimated by planner statistics.
    # Non-target right-side rows never participate in the failure set.
    return """
      WITH targets AS MATERIALIZED (
        SELECT * FROM jsonb_to_recordset(CAST(:targets AS jsonb))
        AS t(user_id uuid,through bigint,generation bigint,epoch bigint)),
      normal AS MATERIALIZED (
        SELECT t.user_id,t.generation,t.epoch,COALESCE(max(m.id),0) last_id,
          COALESCE(max(m.turn_seq),0) last_turn,
          count(*) FILTER(WHERE m.sender='user' AND m.turn_seq IS NULL) unsequenced
        FROM targets t LEFT JOIN messages m ON m.user_id=t.user_id
          AND m.id<=t.through AND m.kind='normal'
        GROUP BY t.user_id,t.generation,t.epoch),
      context_checks AS MATERIALIZED (
        SELECT n.user_id,n.generation,cc.memory_generation actual_generation
        FROM normal n FULL JOIN chat_contexts cc ON cc.user_id=n.user_id),
      pipeline_checks AS MATERIALIZED (
        SELECT n.user_id,n.epoch,n.last_id,n.last_turn,p.mode,p.bootstrap_status,
          p.privacy_epoch,p.consolidated_through_turn_seq
        FROM normal n FULL JOIN memory_pipeline_states p ON p.user_id=n.user_id),
      contract_checks AS MATERIALIZED (
        SELECT n.user_id,n.last_id,c.processed_through_message_id
        FROM normal n FULL JOIN (
          SELECT user_id,memory_generation,processed_through_message_id
          FROM retention_prepared_contracts WHERE preparation_id=:id AND status='ready'
        ) c ON c.user_id=n.user_id AND c.memory_generation=n.generation),
      blocked_plans AS MATERIALIZED (
        SELECT DISTINCT n.user_id FROM normal n JOIN mem0_ingest_candidates x
          ON x.user_id=n.user_id AND x.turn_seq<=n.last_turn AND x.status='planned'
        WHERE n.last_id>0),
      blocked_facts AS MATERIALIZED (
        SELECT DISTINCT n.user_id FROM normal n JOIN mem0_memory_registry r
          ON r.user_id=n.user_id AND r.source_turn_seq<=n.last_turn
         AND (r.semantic_status='pending' OR
              (r.semantic_status='active' AND r.canonical_text IS NULL))
        WHERE n.last_id>0)
      SELECT NOT EXISTS (
        SELECT user_id FROM context_checks WHERE user_id IS NOT NULL
          AND (actual_generation IS NULL OR generation IS NULL
               OR actual_generation IS DISTINCT FROM generation)
        UNION ALL
        SELECT user_id FROM pipeline_checks WHERE user_id IS NOT NULL AND last_id>0
          AND (mode IS DISTINCT FROM 'v2' OR bootstrap_status IS DISTINCT FROM 'ready'
            OR privacy_epoch IS DISTINCT FROM epoch
            OR COALESCE(consolidated_through_turn_seq,0)<last_turn)
        UNION ALL
        SELECT user_id FROM contract_checks WHERE user_id IS NOT NULL AND last_id>0
          AND COALESCE(processed_through_message_id,0)<last_id
        UNION ALL
        SELECT user_id FROM normal WHERE last_id>0 AND unsequenced<>0
        UNION ALL SELECT user_id FROM blocked_plans
        UNION ALL SELECT user_id FROM blocked_facts)
      """, {"targets": json.dumps(rows), "id": identity}


async def processing_set_complete(session, identity, manifest):
    if not any(record["through"] for record in manifest["users"].values()):
        return True
    statement, params = processing_statement(identity, manifest)
    return await session.scalar(text(statement), params) is True
