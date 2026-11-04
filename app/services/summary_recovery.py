"""Verify replacement of exhausted legacy summaries; never replay or mutate jobs.

Only a published checkpoint backed by a successful job with the complete failed
source set can settle an incident. A larger numeric boundary is not evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import uuid

from sqlalchemy import text

from app.services import checkpoint

ERRORS = frozenset({"summary_invalid", "summary_llm_failed"})
VERSIONS = frozenset({checkpoint.LEGACY_SUMMARIZER_VERSION, checkpoint.SUMMARIZER_VERSION})
RECEIPT_KEY = "monitoring:summary_recovery_receipts:v1"
MAX_RECEIPTS = 1024
MAX_FAILURES = 50
MAX_CANDIDATES = 20
MAX_SOURCE_ROWS = 4000
GRACE = timedelta(minutes=15)


def grouped_failure(job_type, error_code):
    return job_type == checkpoint.JOB_CONVERSATION_CHECKPOINT and error_code in ERRORS


def _dict(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def _wire_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _identity(value):
    return uuid.UUID(str(value))


def _manifest(job, *, current_generation, parents, source_rows):
    """Recompute the exact producer hash from original source and parent rows."""
    payload = _dict(job.get("payload"))
    generation = payload.get("memory_generation")
    ids = payload.get("source_message_ids")
    version = payload.get("summarizer_version")
    if (
        payload.get("schema_version") != checkpoint.SCHEMA_VERSION
        or type(generation) is not int
        or generation < 0
        or generation != current_generation
        or version not in VERSIONS
        or payload.get("retention_preparation_id") is not None
        or payload.get("mode") is not None
        or not isinstance(ids, list)
        or not ids
        or len(ids) > MAX_SOURCE_ROWS
        or any(type(mid) is not int or mid <= 0 for mid in ids)
        or len(set(ids)) != len(ids)
        or ids != sorted(ids)
        or type(payload.get("through_message_id")) is not int
        or payload["through_message_id"] != ids[-1]
        or not isinstance(payload.get("source_hash"), str)
        or re.fullmatch(r"[0-9a-f]{64}", payload["source_hash"]) is None
        or job.get("payload_redacted_at") is not None
        or _wire_hash(payload) != job.get("payload_hash")
    ):
        return None
    owner = _identity(job["user_id"])
    selected = []
    for mid in ids:
        row = source_rows.get(mid)
        if row is None or _identity(row["user_id"]) != owner:
            return None
        selected.append(
            checkpoint.SourceMessage(
                mid, row["sender"], row["kind"], row["content"], row.get("created_at")
            )
        )
    previous = None
    previous_id = payload.get("previous_checkpoint_id")
    if previous_id is not None:
        parent = parents.get(str(_identity(previous_id)))
        if (
            parent is None
            or _identity(parent["user_id"]) != owner
            or parent["memory_generation"] != generation
            or parent["publish_state"] != "published"
            or payload.get("previous_through_message_id") != parent["through_message_id"]
            or parent["through_message_id"] >= ids[0]
        ):
            return None
        previous = checkpoint.Checkpoint(
            _identity(parent["id"]),
            parent["through_message_id"],
            parent["summary"],
            parent["version"],
            parent["source_hash"],
        )
    elif payload.get("previous_through_message_id") is not None:
        return None
    if (
        checkpoint.source_hash(previous=previous, messages=selected, version=version)
        != payload["source_hash"]
    ):
        return None
    return payload, frozenset(ids)


def verified_replacement(failed, successful, publication, *, generation, parents, source_rows):
    """No I/O and no model: same owner/generation/version, exact hashes, superset."""
    try:
        if (
            failed.get("state") != "dead"
            or failed.get("queue") != "content"
            or successful.get("state") != "succeeded"
            or not grouped_failure(failed.get("job_type"), failed.get("last_error_code"))
            or successful.get("job_type") != checkpoint.JOB_CONVERSATION_CHECKPOINT
            or type(failed.get("attempt")) is not int
            or type(failed.get("max_attempts")) is not int
            or failed["attempt"] < failed["max_attempts"]
            or _identity(failed["user_id"]) != _identity(successful["user_id"])
            or _identity(failed["user_id"]) != _identity(publication["user_id"])
            or publication["memory_generation"] != generation
            or publication["publish_state"] != "published"
            or publication.get("preparation_id") is not None
            or not publication.get("summary", "").strip()
            or publication["created_at"] < failed["finished_at"]
        ):
            return False
        failed_manifest = _manifest(
            failed, current_generation=generation, parents=parents, source_rows=source_rows
        )
        successful_manifest = _manifest(
            successful, current_generation=generation, parents=parents, source_rows=source_rows
        )
        if failed_manifest is None or successful_manifest is None:
            return False
        old, wanted = failed_manifest
        new, covered = successful_manifest
        return (
            old["summarizer_version"] == new["summarizer_version"] == publication["version"]
            and publication["source_hash"] == new["source_hash"]
            and publication["through_message_id"] == new["through_message_id"]
            and wanted <= covered
        )
    except (ValueError, TypeError, KeyError, AttributeError, checkpoint.CheckpointError):
        return False


def receipt_for(failed, successful, publication, now):
    """Only immutable job/checkpoint identifiers and hashes; no owner/source/body."""
    return {
        "schema": "summary-recovery-v1",
        "failed_payload_hash": failed["payload_hash"],
        "success_payload_hash": successful["payload_hash"],
        "checkpoint_id": str(publication["id"]),
        "source_hash": publication["source_hash"],
        "verified_at": now.isoformat(),
    }


def receipt_matches(job, receipt):
    try:
        return (
            isinstance(receipt, dict)
            and set(receipt)
            == {
                "schema",
                "failed_payload_hash",
                "success_payload_hash",
                "checkpoint_id",
                "source_hash",
                "verified_at",
            }
            and receipt.get("schema") == "summary-recovery-v1"
            and all(
                isinstance(receipt.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", receipt[key])
                for key in ("failed_payload_hash", "success_payload_hash", "source_hash")
            )
            and str(_identity(receipt["checkpoint_id"])) == receipt["checkpoint_id"]
            and datetime.fromisoformat(receipt["verified_at"]).tzinfo is not None
            and receipt["failed_payload_hash"] == job.get("payload_hash")
        )
    except (ValueError, TypeError, KeyError):
        return False


@dataclass(frozen=True)
class Snapshot:
    failures: tuple[dict, ...]
    recovered: frozenset[str]
    new_receipts: dict
    complete: bool
    now: datetime

    @property
    def pending(self):
        return tuple(
            job
            for job in self.failures
            if str(job["id"]) not in self.recovered
            and (
                not isinstance(job.get("finished_at"), datetime)
                or self.now - job["finished_at"] >= GRACE
            )
        )

    @property
    def waiting(self):
        return len(self.failures) - len(self.recovered) - len(self.pending)


_FAILURES = text("""WITH RECURSIVE resolved(id) AS (
 SELECT replay_of FROM async_jobs WHERE state='succeeded' AND replay_of IS NOT NULL
 UNION SELECT j.replay_of FROM async_jobs j JOIN resolved x ON j.id=x.id WHERE j.replay_of IS NOT NULL)
 SELECT j.id,j.user_id,j.queue,j.job_type,j.state,j.attempt,j.max_attempts,j.payload,j.payload_hash,
 j.payload_redacted_at,j.finished_at,j.last_error_code,cc.memory_generation AS current_generation
 FROM async_jobs j LEFT JOIN chat_contexts cc ON cc.user_id=j.user_id
 WHERE j.state='dead' AND j.queue='content' AND j.job_type='conversation_checkpoint'
 AND j.last_error_code=ANY(CAST(:errors AS text[])) AND NOT EXISTS(SELECT 1 FROM resolved WHERE id=j.id)
 ORDER BY j.finished_at DESC NULLS LAST,j.id LIMIT :limit""")


async def snapshot(session, *, now=None):
    from app.config import settings
    from app.services import privacy

    now = now or datetime.now(timezone.utc)
    failures = [
        dict(row)
        for row in (
            await session.execute(_FAILURES, {"errors": sorted(ERRORS), "limit": MAX_FAILURES + 1})
        )
        .mappings()
        .all()
    ]
    complete = len(failures) <= MAX_FAILURES
    failures = failures[:MAX_FAILURES]
    receipts = _dict(
        await session.scalar(
            text("SELECT value FROM app_config WHERE key=:key"), {"key": RECEIPT_KEY}
        )
    )
    if len(receipts) > MAX_RECEIPTS:
        complete = False
    recovered, added = set(), {}
    for failed in failures:
        identity = str(failed["id"])
        if receipt_matches(failed, receipts.get(identity)):
            recovered.add(identity)
            continue
        if failed["user_id"] is None or failed["current_generation"] is None:
            continue
        barrier = await privacy.load_barrier(session, failed["user_id"])
        if not privacy.authorize_job(
            barrier,
            job_type=checkpoint.JOB_CONVERSATION_CHECKPOINT,
            payload_epoch=_dict(failed["payload"]).get("privacy_epoch"),
            mode=settings.privacy_barrier_mode,
        ).allowed:
            continue
        candidates = (
            (
                await session.execute(
                    text("""SELECT j.id,j.user_id,j.job_type,j.state,j.payload,
          j.payload_hash,j.payload_redacted_at,c.id AS checkpoint_id FROM async_jobs j
          JOIN conversation_checkpoints c ON c.user_id=j.user_id AND c.publish_state='published'
            AND c.source_hash=j.payload->>'source_hash' AND c.version=j.payload->>'summarizer_version'
            AND c.memory_generation=:generation AND c.preparation_id IS NULL
          WHERE j.user_id=:uid AND j.queue='content' AND j.job_type='conversation_checkpoint' AND j.state='succeeded'
            AND j.payload_redacted_at IS NULL AND c.created_at>=:finished
          ORDER BY c.created_at DESC,c.id LIMIT :limit"""),
                    {
                        "uid": failed["user_id"],
                        "generation": failed["current_generation"],
                        "finished": failed["finished_at"],
                        "limit": MAX_CANDIDATES,
                    },
                )
            )
            .mappings()
            .all()
        )
        if not candidates:
            continue
        payloads = [_dict(failed["payload"]), *(_dict(row["payload"]) for row in candidates)]
        if any(not isinstance(payload.get("source_message_ids"), list) for payload in payloads):
            continue
        ids = {
            mid
            for payload in payloads
            for mid in payload.get("source_message_ids", [])
            if type(mid) is int
        }
        if len(ids) > MAX_SOURCE_ROWS:
            complete = False
            continue
        source_rows = {
            row["id"]: dict(row)
            for row in (
                await session.execute(
                    text(
                        "SELECT id,user_id,sender,kind,content,created_at FROM messages WHERE user_id=:uid "
                        "AND id=ANY(CAST(:ids AS bigint[]))"
                    ),
                    {"uid": failed["user_id"], "ids": sorted(ids)},
                )
            )
            .mappings()
            .all()
        }
        parent_ids = {
            payload.get("previous_checkpoint_id")
            for payload in payloads
            if payload.get("previous_checkpoint_id")
        }
        cp_ids = set(parent_ids) | {str(row["checkpoint_id"]) for row in candidates}
        try:
            cp_ids = [uuid.UUID(str(value)) for value in cp_ids]
        except ValueError:
            continue
        checkpoints = {
            str(row["id"]): dict(row)
            for row in (
                await session.execute(
                    text(
                        "SELECT * FROM conversation_checkpoints WHERE user_id=:uid AND id=ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"uid": failed["user_id"], "ids": cp_ids},
                )
            )
            .mappings()
            .all()
        }
        for raw in candidates:
            successful = dict(raw)
            publication = checkpoints.get(str(raw["checkpoint_id"]))
            if publication and verified_replacement(
                failed,
                successful,
                publication,
                generation=failed["current_generation"],
                parents=checkpoints,
                source_rows=source_rows,
            ):
                if identity in receipts or len(receipts) + len(added) < MAX_RECEIPTS:
                    recovered.add(identity)
                    added[identity] = receipt_for(failed, successful, publication, now)
                else:
                    complete = False
                break
    return Snapshot(tuple(failures), frozenset(recovered), added, complete, now)


async def persist_receipts(session, values):
    """Merge verified metadata only. Caller commits; no terminal job is touched."""
    if not values:
        return
    if len(values) > MAX_RECEIPTS:
        raise ValueError("summary_recovery_receipt_capacity")
    for identity, receipt in values.items():
        if str(uuid.UUID(identity)) != identity or not receipt_matches(
            {"payload_hash": receipt.get("failed_payload_hash")}, receipt
        ):
            raise ValueError("summary_recovery_receipt_invalid")
    result = await session.execute(
        text("""INSERT INTO app_config(key,value) VALUES(:key,CAST(:value AS jsonb))
      ON CONFLICT(key) DO UPDATE SET value=app_config.value||EXCLUDED.value,updated_at=now()
      WHERE (SELECT count(*) FROM jsonb_object_keys(app_config.value||EXCLUDED.value))<=:cap
      RETURNING key"""),
        {"key": RECEIPT_KEY, "value": json.dumps(values), "cap": MAX_RECEIPTS},
    )
    if result.first() is None:
        raise ValueError("summary_recovery_receipt_capacity")


async def resolved_receipt_ids(session):
    """Health checks read tiny historical receipts; no dialogue/body proof scan."""
    values = _dict(
        await session.scalar(
            text("SELECT value FROM app_config WHERE key=:key"), {"key": RECEIPT_KEY}
        )
    )
    if len(values) > MAX_RECEIPTS:
        return frozenset()
    identities = []
    for value in values:
        try:
            identities.append(uuid.UUID(value))
        except (TypeError, ValueError):
            continue
    if not identities:
        return frozenset()
    rows = (
        (
            await session.execute(
                text("""SELECT id,payload_hash FROM async_jobs
        WHERE id=ANY(CAST(:ids AS uuid[])) AND state='dead' AND queue='content' AND job_type='conversation_checkpoint'
        AND last_error_code=ANY(CAST(:errors AS text[]))"""),
                {"ids": identities, "errors": sorted(ERRORS)},
            )
        )
        .mappings()
        .all()
    )
    return frozenset(
        str(row["id"]) for row in rows if receipt_matches(row, values.get(str(row["id"])))
    )
