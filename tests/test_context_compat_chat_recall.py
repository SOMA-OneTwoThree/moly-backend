"""Actual recall composition certifies required + optional legacy NULL separately."""
import hashlib
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.core import db
from app.services import chat, context_retention, mem0_ingest, mem0_recall, provided_memory


@pytest.mark.parametrize("required_null,optional_null", [(True,True),(True,False),(False,True),(False,False)])
@pytest.mark.parametrize("scenario", ["normal", "cap_at", "cap_over", "source_kind", "sender", "authority", "source_hash", "canonical_hash", "generation"])
async def test_mixed_recall_preserves_optional_proof_and_combined_cap_without_duplicate_queries(
    monkeypatch,required_null,optional_null,scenario,
):
    uid,rid_required,rid_optional,pid_required,pid_optional=(uuid.uuid4() for _ in range(5))
    required_text,optional_text="Synthetic required fact","Synthetic optional fact"
    timestamp=datetime(2026,11,5,tzinfo=timezone.utc)
    values=[(rid_required,pid_required,required_text,"required",required_null,1),
            (rid_optional,pid_optional,optional_text,"related",optional_null,2)]
    calls=[]
    captured=[]
    class Rows:
        def __init__(self,rows):
            self.rows=rows
        def all(self):
            return self.rows
        def mappings(self):
            return self
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self,*args):
            pass
        async def execute(self,sql,params=None):
            calls.append((sql,params))
            assert sql is not context_retention._REQUIRED
            assert sql is not context_retention._REQUIRED_SOURCES
            assert sql is not context_retention._REQUIRED_LEGACY_CANONICAL
            if sql is provided_memory._ROWS:
                assert set(params["ids"])=={rid_optional}
                if scenario == "generation":
                    return Rows([])
                return Rows([dict(registry_id=rid_optional,provider_memory_id=pid_optional,
                    provider="mem0",collection_version="v2",canonical_text=None if optional_null else optional_text,
                    content_hash=mem0_ingest.candidate_hash("different") if scenario == "canonical_hash" else mem0_ingest.candidate_hash(optional_text),occurred_at=timestamp)])
            if sql is provided_memory._SOURCES:
                assert set(params["ids"])=={rid_optional}
                return Rows([dict(registry_id=rid_optional,source_message_id=2,source_sender="user",
                    authority="inferred" if scenario == "authority" else "explicit_user",start_utf8=0,end_utf8=len(optional_text.encode()),
                    span_hash="0"*64 if scenario == "source_hash" else hashlib.sha256(optional_text.encode()).hexdigest(),source_occurred_at=timestamp,
                    actual_message_id=2,message_sender="moly" if scenario == "sender" else "user",
                    message_kind="safety" if scenario == "source_kind" else "normal",message_content=optional_text)])
            assert params is None  # The actual registry SELECT is the only remaining statement.
            return Rows([(rid,pid,1,mem0_ingest.candidate_hash(body),None if absent else body,priority)
                for rid,pid,body,priority,absent,_ in values])
    async def recall(*args,**kwargs):
        return [mem0_recall.Recalled(body,"active",0.1,timestamp,None,str(pid))
                for _,pid,body,_,_,_ in values]
    original_capture=provided_memory.capture
    async def capture(*args,**kwargs):
        captured.append(kwargs)
        return await original_capture(*args,**kwargs)
    monkeypatch.setattr(db,"get_sessionmaker",lambda:Session)
    monkeypatch.setattr(chat,"_recall_adapter",lambda:object())
    monkeypatch.setattr(mem0_recall,"recall",recall)
    monkeypatch.setattr(provided_memory,"capture",capture)
    snapshots=[]
    mandatory = required_text
    if scenario in {"cap_at", "cap_over"}:
        mandatory = "x" * (2048 - 1 - len(optional_text.encode()) + int(scenario == "cap_over"))
    block=await chat._recall_memory_v2(uid,query="synthetic recall",state=SimpleNamespace(serves_v2=True),
        language="en",today=date(2026,11,5),tz_name="UTC",retention_enabled=True,
        generation=0,registry_snapshot=snapshots,mandatory_facts=mandatory)
    if scenario in {"normal", "cap_at"}:
        assert optional_text in block and required_text not in block
        assert len(snapshots)==1 and snapshots[0].registry_id==rid_optional
        assert snapshots[0].canonical_from_legacy is optional_null
    else:
        assert block == chat._memory_unavailable_notice("en") and snapshots == []
    assert len(captured)==1
    assert captured[0]["canonical_overrides"]==({rid_optional:optional_text} if optional_null else {})
    assert all(sql is not statement for sql,_ in calls for statement in
               (context_retention._REQUIRED,context_retention._REQUIRED_SOURCES,context_retention._REQUIRED_LEGACY_CANONICAL))
