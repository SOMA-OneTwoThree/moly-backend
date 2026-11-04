"""Existing dev outer rollback; only generated fixture owners and terminal jobs.

Run by the root's serial DB verifier. No Slack/provider call or runnable job.
The test-only source query is restricted to the fixture UUID before any body read.
"""

import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.services import checkpoint, summary_recovery as recovery
from tests.integration.test_checkpoint_continuity_dev import checkpoint_db as checkpoint_db


async def seed(session, uid, monkeypatch):
    monkeypatch.setattr(recovery, "RECEIPT_KEY", f"monitoring:test:summary:{uuid.uuid4().hex}")
    original = str(recovery._FAILURES)
    monkeypatch.setattr(
        recovery,
        "_FAILURES",
        text(
            original.replace(
                "ORDER BY j.finished_at", f"AND j.user_id='{uid}'::uuid ORDER BY j.finished_at"
            )
        ),
    )
    await session.execute(
        text(
            "INSERT INTO privacy_subject_barriers(user_id,state,epoch) "
            "VALUES(:u,'active',0) ON CONFLICT(user_id) DO UPDATE SET state='active',epoch=0"
        ),
        {"u": uid},
    )
    await session.execute(
        text("UPDATE chat_contexts SET memory_generation=0 WHERE user_id=:u"), {"u": uid}
    )
    now = await session.scalar(text("SELECT now()"))
    rows = []
    for position in range(6):
        row = (
            (
                await session.execute(
                    text(
                        "INSERT INTO messages(user_id,sender,kind,content,activity_date,turn_seq,turn_position) "
                        "VALUES(:u,:sender,'normal','Synthetic recovery source',CURRENT_DATE,:seq,:position) "
                        "RETURNING id,user_id,sender,kind,content,created_at"
                    ),
                    {
                        "u": uid,
                        "sender": "user" if position % 2 == 0 else "moly",
                        "seq": position // 2 + 1,
                        "position": position % 2 + 1,
                    },
                )
            )
            .mappings()
            .one()
        )
        rows.append(dict(row))

    def payload(selected):
        return {
            "schema_version": checkpoint.SCHEMA_VERSION,
            "summarizer_version": checkpoint.SUMMARIZER_VERSION,
            "memory_generation": 0,
            "source_message_ids": [row["id"] for row in selected],
            "through_message_id": selected[-1]["id"],
            "previous_checkpoint_id": None,
            "previous_through_message_id": None,
            "source_hash": checkpoint.source_hash(
                previous=None,
                messages=[
                    checkpoint.SourceMessage(
                        row["id"], row["sender"], row["kind"], row["content"], row["created_at"]
                    )
                    for row in selected
                ],
            ),
        }

    async def job(selected, state):
        value = payload(selected)
        identity = await session.scalar(
            text(
                "INSERT INTO async_jobs(queue,job_type,user_id,dedup_key,payload,payload_hash,"
                "state,attempt,max_attempts,last_error_code,result_code,finished_at) "
                "VALUES('content','conversation_checkpoint',:u,:key,CAST(:payload AS jsonb),:hash,:state,:attempt,3,:error,:result,:at) "
                "RETURNING id"
            ),
            {
                "u": uid,
                "key": str(uuid.uuid4()),
                "payload": json.dumps(value),
                "hash": recovery._wire_hash(value),
                "state": state,
                "attempt": 3 if state == "dead" else 1,
                "error": "summary_invalid" if state == "dead" else None,
                "result": "summary_invalid" if state == "dead" else "ok",
                "at": now - timedelta(minutes=20) if state == "dead" else now,
            },
        )
        return identity, value

    failed, failed_payload = await job(rows[:4], "dead")
    successful, value = await job(rows, "succeeded")
    cp = await session.scalar(
        text(
            "INSERT INTO conversation_checkpoints(user_id,through_message_id,summary,version,"
            "source_hash,memory_generation,publish_state) VALUES(:u,:through,'Synthetic recovered summary',:version,:hash,0,'published') "
            "RETURNING id"
        ),
        {
            "u": uid,
            "through": value["through_message_id"],
            "version": checkpoint.SUMMARIZER_VERSION,
            "hash": value["source_hash"],
        },
    )
    return failed, successful, cp, rows, failed_payload, now


async def test_actual_verified_superset_receipt_preserves_dead_and_budget(
    checkpoint_db, monkeypatch
):
    session, (uid, _) = checkpoint_db
    failed, _, _, _, _, now = await seed(session, uid, monkeypatch)
    snapshot = await recovery.snapshot(session, now=now)
    assert snapshot.recovered == {str(failed)} and snapshot.complete and not snapshot.pending
    await recovery.persist_receipts(session, snapshot.new_receipts)
    assert await recovery.resolved_receipt_ids(session) == {str(failed)}
    row = (
        await session.execute(
            text("SELECT state,attempt,max_attempts FROM async_jobs WHERE id=:id"), {"id": failed}
        )
    ).one()
    assert tuple(row) == ("dead", 3, 3)


async def test_actual_receipt_survives_expected_payload_redaction_and_cp_removal(
    checkpoint_db, monkeypatch
):
    session, (uid, _) = checkpoint_db
    failed, success, cp, _, _, now = await seed(session, uid, monkeypatch)
    snapshot = await recovery.snapshot(session, now=now)
    await recovery.persist_receipts(session, snapshot.new_receipts)
    await session.execute(
        text(
            "UPDATE async_jobs SET payload='{}'::jsonb,payload_redacted_at=now() WHERE id=ANY(CAST(:ids AS uuid[]))"
        ),
        {"ids": [failed, success]},
    )
    await session.execute(text("DELETE FROM conversation_checkpoints WHERE id=:id"), {"id": cp})
    again = await recovery.snapshot(session, now=now)
    assert again.recovered == {str(failed)} and not again.new_receipts
    assert await recovery.resolved_receipt_ids(session) == {str(failed)}


@pytest.mark.parametrize("fault", ["source", "generation", "unpublished", "payload_hash"])
async def test_actual_unverified_source_never_clears_incident(checkpoint_db, monkeypatch, fault):
    session, (uid, _) = checkpoint_db
    failed, _, cp, rows, _, now = await seed(session, uid, monkeypatch)
    if fault == "source":
        await session.execute(
            text("UPDATE messages SET content='Synthetic changed source' WHERE id=:id"),
            {"id": rows[0]["id"]},
        )
    elif fault == "generation":
        await session.execute(
            text("UPDATE chat_contexts SET memory_generation=1 WHERE user_id=:u"), {"u": uid}
        )
    elif fault == "unpublished":
        await session.execute(
            text("UPDATE conversation_checkpoints SET publish_state='superseded' WHERE id=:id"),
            {"id": cp},
        )
    else:
        await session.execute(
            text("UPDATE async_jobs SET payload_hash=repeat('0',64) WHERE id=:id"), {"id": failed}
        )
    snapshot = await recovery.snapshot(session, now=now)
    assert not snapshot.recovered and not snapshot.new_receipts
    assert {str(job["id"]) for job in snapshot.pending} == {str(failed)}
