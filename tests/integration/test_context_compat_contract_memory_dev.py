"""Existing development DB only. Owned synthetic subjects; fixture rolls back all rows."""
import hashlib
import json
import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.services import context_compat, contract_repo, context_retention, interaction_contract as ic
from app.services import provided_memory
from app.services.mem0_ingest import candidate_hash
from tests.integration.test_checkpoint_continuity_dev import checkpoint_db as checkpoint_db
from tests.integration.test_provided_memory_dev import seed_memory, CANONICAL


async def contract_insert(session, uid, *, version, document, validation=None):
    rendered = ic.render_document([contract_repo.directive_from_item(item) for item in document], language="ko") if document else ""
    return (await session.execute(text("""INSERT INTO user_interaction_contracts
      (user_id,version,locale,document_json,rendered_text,render_hash,status,validation_json)
      VALUES(:uid,:version,'ko',CAST(:doc AS jsonb),:rendered,:hash,'draft',CAST(:validation AS jsonb))
      RETURNING id"""), {"uid": uid,"version": version,"doc": json.dumps(document),
        "rendered": rendered,"hash": hashlib.sha256(rendered.encode()).hexdigest(),
        "validation": json.dumps(validation) if validation is not None else None})).scalar_one()


async def test_actual_legacy_delta_late_legacy_union_and_stop_remain_source_proven(checkpoint_db):
    session, (uid, _) = checkpoint_db
    _, mid = await seed_memory(session, uid)
    document = [dict(kind="response_style",action="prefer",condition="always",target_tag="brief",source_message_id=mid),
                dict(kind="address",action="use",condition="always",target_literal="Synthetic",source_message_id=mid)]
    old = await contract_insert(session, uid, version=1, document=document)
    await contract_repo.publish(session, contract_id=old, user_id=uid, locale="ko")
    b = context_compat.Baseline(str(uuid.uuid4()),str(uid),0,0,mid,mid,None,0,None,None,None,
        0,str(old),context_compat.digest(document))
    await session.execute(text("UPDATE chat_contexts SET retention_compat_json=CAST(:b AS jsonb) WHERE user_id=:u"),
                          {"u": uid,"b": json.dumps(b.payload())})
    end = "stop that tone"
    stop = (await session.execute(text("""INSERT INTO messages(user_id,sender,kind,content,activity_date,turn_seq,turn_position)
      VALUES(:u,'user','normal',:body,CURRENT_DATE,2,1) RETURNING id"""), {"u": uid,"body": end})).scalar_one()
    initial = await contract_repo.retained_document(session, user_id=uid,generation=0,compat_baseline=b)
    assert len(initial) == 2 and all("generation" not in item for item in initial)
    projected = contract_repo.apply_legacy_ends(initial,[SimpleNamespace(id=stop,sender="user",content=end)])
    native, metadata = contract_repo.compatibility_metadata(projected,b,after=mid,through=stop,
        source_rows=[(stop,"user",end,date.today())])
    delta = await contract_insert(session,uid,version=2,document=native,validation=metadata)
    await contract_repo.publish(session,contract_id=delta,user_id=uid,locale="ko")
    late = await contract_insert(session,uid,version=3,document=[document[0]])
    await contract_repo.publish(session,contract_id=late,user_id=uid,locale="ko")
    # The late document both repeats a stopped item and omits another old agreement.
    loaded = await contract_repo.retained_document(session,user_id=uid,generation=0,compat_baseline=b)
    assert [item["kind"] for item in contract_repo.active_retained_items(loaded)] == ["address"]
    for enabled in (True,False):
        result = await contract_repo.published_text(session,user_id=uid,locale="en",generation=0,
            retention_enabled=enabled,compat_baseline=b)
        assert "Synthetic" in result and "brief" not in result
    assert (await session.execute(text("SELECT document_json FROM user_interaction_contracts WHERE id=:i"),{"i":old})).scalar_one() == document
    # A separate existing locale must not become the latest legacy head.
    foreign = await contract_insert(session,uid,version=9,document=[dict(document[0],target_tag="detailed")])
    await session.execute(text("UPDATE user_interaction_contracts SET locale='en',status='published' WHERE id=:i"),{"i":foreign})
    assert [item["kind"] for item in contract_repo.active_retained_items(await contract_repo.retained_document(
        session,user_id=uid,generation=0,compat_baseline=b))] == ["address"]
    await session.execute(text("UPDATE messages SET kind='safety_check' WHERE id=:i"),{"i":stop})
    with pytest.raises(ic.ContractViolation):
        await contract_repo.retained_document(session,user_id=uid,generation=0,compat_baseline=b)


async def seed_null(session,uid):
    rid, mid = await seed_memory(session,uid)
    await session.execute(text("UPDATE mem0_memory_registry SET canonical_text=NULL,context_priority='required' WHERE id=:r"),{"r":rid})
    pid = (await session.execute(text("SELECT provider_memory_id FROM mem0_memory_registry WHERE id=:r"),{"r":rid})).scalar_one()
    return rid,pid,mid


async def test_actual_required_null_committed_candidate_and_optional_hit_are_read_only(checkpoint_db):
    session,(uid,_) = checkpoint_db
    rid,pid,mid = await seed_null(session,uid)
    await session.execute(text("""INSERT INTO mem0_ingest_candidates(user_id,turn_seq,candidate_hash,schema_version,
      extractor_version,normalizer_version,provider_memory_id,candidate_text,status)
      VALUES(:u,1,:hash,'v1','synthetic','norm-v1',:pid,:body,'committed')"""),
      {"u":uid,"hash":candidate_hash(CANONICAL),"pid":pid,"body":CANONICAL})
    assert await context_retention.load_required_facts(session,user_id=uid,generation=0) == CANONICAL
    proof = await provided_memory.capture(session,user_id=uid,generation=0,ids=[rid],canonical_overrides={rid:CANONICAL})
    assert proof[0].canonical_from_legacy and await provided_memory.validate(session,user_id=uid,generation=0,snapshots=proof)
    assert (await session.execute(text("SELECT canonical_text FROM mem0_memory_registry WHERE id=:r"),{"r":rid})).scalar_one() is None
    await session.execute(text("UPDATE messages SET kind='safety' WHERE id=:m"),{"m":mid})
    assert not await provided_memory.validate(session,user_id=uid,generation=0,snapshots=proof)
    with pytest.raises(context_retention.RetentionError,match="required_source_excluded"):
        await context_retention.load_required_facts(session,user_id=uid,generation=0)


async def test_actual_required_null_vector_owner_hash_and_original_identity_are_strict(checkpoint_db):
    session,(uid,other) = checkpoint_db
    rid,pid,_ = await seed_null(session,uid)
    # Dimension is obtained from the column type only; no existing vectors/bodies are read.
    dimension = (await session.execute(text("""SELECT atttypmod FROM pg_attribute
      WHERE attrelid='vecs.moly_memories_v2'::regclass AND attname='vec'"""))).scalar_one()
    await session.execute(text("""INSERT INTO vecs.moly_memories_v2(id,vec,metadata)
      VALUES(:id,CAST(:vec AS vector),CAST(:metadata AS jsonb))"""),
      {"id":str(pid),"vec":"["+",".join(["0"]*dimension)+"]",
       "metadata":json.dumps({"user_id":str(uid),"data":CANONICAL})})
    assert await context_retention.load_required_facts(session,user_id=uid,generation=0) == CANONICAL
    await session.execute(text("UPDATE vecs.moly_memories_v2 SET metadata=jsonb_build_object('user_id',CAST(:other AS text),'data',CAST(:body AS text)) WHERE id=:pid"),
        {"other":str(other),"body":CANONICAL,"pid":str(pid)})
    with pytest.raises(context_retention.RetentionError,match="required_legacy_unproved"):
        await context_retention.load_required_facts(session,user_id=uid,generation=0)
