"""Existing dev DB only; fallback control writes remain inside fixture rollback."""
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from tests.integration.test_checkpoint_continuity_dev import checkpoint_db as checkpoint_db
from worker import contract_jobs
from worker.consumer import JobCancelled


@pytest.mark.parametrize("changed", [None, "lease", "generation", "payload", "expired"])
async def test_fallback_control_requires_current_job_and_source_generation(checkpoint_db, monkeypatch, changed):
    session, (uid, _) = checkpoint_db
    identity, token = uuid.uuid4(), uuid.uuid4()
    before = {"retention_enabled": True, "memory_generation": 0, "privacy_epoch": 0,
              "through_message_id": 1, "adaptive_output_fallback": True,
              "adaptive_output_attempts_remaining": 2}
    after = {**before, "adaptive_output_attempts_remaining": 1}
    await session.execute(text("""
      INSERT INTO privacy_subject_barriers(user_id,state,epoch) VALUES(:u,'active',0) ON CONFLICT DO NOTHING
    """), {"u": uid})
    await session.execute(text("""
      INSERT INTO async_jobs(id,queue,job_type,user_id,dedup_key,payload,state,attempt,max_attempts,
        lease_owner,lease_token,lease_until,payload_expires_at)
      VALUES(:j,'memory','contract_compile',:u,:dedup,CAST(:p AS jsonb),'running',1,2,
        'synthetic-fallback',:t,now()+interval '1 minute',now()+interval '1 hour')
    """), {"j": identity, "u": uid, "dedup": str(identity), "p": json.dumps(before), "t": token})
    if changed == "lease":
        await session.execute(text("UPDATE async_jobs SET lease_token=:t WHERE id=:j"),
                              {"t": uuid.uuid4(), "j": identity})
    elif changed == "generation":
        await session.execute(text("UPDATE chat_contexts SET memory_generation=1 WHERE user_id=:u"), {"u": uid})
    elif changed == "payload":
        await session.execute(text("UPDATE async_jobs SET payload=payload || CAST(:patch AS jsonb) WHERE id=:j"),
                              {"j": identity, "patch": json.dumps({"changed": True})})
    elif changed == "expired":
        await session.execute(text("UPDATE async_jobs SET payload_expires_at=now()-interval '1 second' WHERE id=:j"), {"j": identity})
    snapshot = (await session.execute(text("SELECT payload FROM async_jobs WHERE id=:j"), {"j": identity})).scalar_one()
    original_commit = session.commit

    @asynccontextmanager
    async def same_session():
        yield session

    async def no_outer_commit():
        await session.flush()

    monkeypatch.setattr(contract_jobs, "get_sessionmaker", lambda: same_session)
    monkeypatch.setattr(session, "commit", no_outer_commit)
    job = SimpleNamespace(id=identity, user_id=uid, lease_owner="synthetic-fallback", lease_token=token)
    if changed is not None:
        with pytest.raises(JobCancelled):
            await contract_jobs._persist_output_fallback(job, before, after)
    else:
        await contract_jobs._persist_output_fallback(job, before, after)
    row = (await session.execute(text("SELECT payload,payload_hash FROM async_jobs WHERE id=:j"), {"j": identity})).one()
    assert row[0] == (snapshot if changed else after)
    if changed is None:
        wire = json.dumps(after, sort_keys=True, separators=(",", ":"))
        assert row[1] == hashlib.sha256(wire.encode()).hexdigest()
    monkeypatch.setattr(session, "commit", original_commit)
