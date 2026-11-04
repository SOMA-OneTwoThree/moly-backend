"""Retention contracts fill their unchanged full-wire budget without skipping source."""
import copy
import hashlib
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import context_retention as retention
from app.services import llm
from tests.test_background_context_budget import contract_environment, job  # noqa: F401
from worker import contract_jobs as worker
from worker.consumer import JobCancelled, JobFatal, JobRetry


def batch(messages, *, previous=None, **kwargs):
    return worker.contract_batch(messages, previous=previous or [], system=worker.cc.RETENTION_SYSTEM,
                                 retention_enabled=True, **kwargs)


def assert_bound(messages, previous=None):
    return retention.assert_input_budget(worker.cc.RETENTION_SYSTEM,
        worker._model_input(messages, previous or []), output_reserve=2000)


def test_1636_korean_characters_fit_one_retention_call_with_the_existing_16k_bound():
    original = worker._Msg(1, "user", "가" * 1636)
    selected = batch([original])
    assert selected.messages == (original,)
    assert selected.next_index == 1 and selected.next_offset == 0
    assert assert_bound(selected.messages) <= 16_384
    legacy = worker.contract_batch([original], previous=[], system=worker.cc.SYSTEM)
    assert legacy.next_index == 0 and 0 < legacy.next_offset <= 2048


async def test_actual_retention_worker_finishes_the_same_long_source_in_one_model_call(request):
    state, session = request.getfixturevalue("contract_environment")
    state["body"] = "가" * 1636
    result = await worker.handle_contract_compile(job({"retention_enabled": True,
        "memory_generation": 0, "through_message_id": 1}))
    assert state["cursor"] == 0
    await result.apply_domain(session)
    assert state["calls"] == 1 and state["cursor"] == 1
    assert not state["queued"]


def test_two_complete_sources_stop_before_a_later_partial_source():
    messages = [worker._Msg(1, "user", "first" * 30), worker._Msg(2, "moly", "second" * 30),
                worker._Msg(3, "user", "large" * 6000)]
    selected = batch(messages)
    assert selected.messages == tuple(messages[:2])
    assert selected.next_index == 2 and selected.next_offset == 0
    following = batch(messages, start_index=selected.next_index)
    assert len(following.messages) == 1 and following.messages[0].id == 3
    assert following.next_index == 2 and following.next_offset > 0


@pytest.mark.parametrize("pattern", ["🙂한글", 'quote"\\\n\t', "ひらがな🙂", "e\u0301🙂"])
def test_adaptive_utf8_prefix_is_maximal_for_the_actual_json_wire_and_resumes_every_byte(pattern):
    original = worker._Msg(1, "user", pattern * 4000)
    body, pieces, offset, index = original.content.encode(), [], 0, 0
    digest = hashlib.sha256(body).hexdigest()
    while index == 0:
        selected = batch([original], start_offset=offset)
        assert_bound(selected.messages)
        part = selected.messages[0]
        assert part.start_utf8 == offset and part.source_hash == digest
        assert part.content == body[part.start_utf8:part.end_utf8].decode()
        assert part.end_utf8 > offset
        pieces.append(part.content)
        if selected.next_offset:
            remaining = body[offset:].decode()
            extended = remaining[:len(part.content) + 1]
            next_part = worker._Msg(1, "user", extended, None, offset,
                offset + len(extended.encode()), digest)
            with pytest.raises(retention.RetentionError, match="context_input_budget"):
                assert_bound([next_part])
        index, offset = selected.next_index, selected.next_offset
    assert offset == 0 and "".join(pieces) == original.content


def test_existing_contract_and_json_escaping_reduce_available_source_room(monkeypatch):
    # Exercise serialized prior-document overhead independently of directive validation.
    monkeypatch.setattr(worker, "_prompt_previous", lambda items, **kwargs: items)
    original = worker._Msg(1, "user", '"\\\n🙂' * 5000)
    previous = [{"item_key": "prior", "scope": "persistent", "text": 'old"\\\n' * 650}]
    before = copy.deepcopy(previous)
    no_previous = batch([original])
    with_previous = batch([original], previous=previous)
    assert with_previous.next_offset < no_previous.next_offset
    assert assert_bound(with_previous.messages, previous) <= 16_384
    assert previous == before


@pytest.mark.parametrize("previous_size", [0, 5000])
def test_old_two_kilobyte_partial_offset_resumes_with_the_same_absolute_source_hash(monkeypatch, previous_size):
    monkeypatch.setattr(worker, "_prompt_previous", lambda items, **kwargs: items)
    previous = [{"item_key": "prior", "text": "x" * previous_size}] if previous_size else []
    original = worker._Msg(1, "user", "가나다🙂" * 1800)
    legacy = worker.contract_batch([original], previous=previous, system=worker.cc.RETENTION_SYSTEM)
    assert 0 < legacy.next_offset <= 2048
    pieces, offset, index = [legacy.messages[0].content], legacy.next_offset, 0
    digest = hashlib.sha256(original.content.encode()).hexdigest()
    while index == 0:
        selected = batch([original], previous=previous, start_offset=offset)
        assert_bound(selected.messages, previous)
        part = selected.messages[0]
        assert part.start_utf8 == offset and part.source_hash == digest
        assert original.content.encode()[part.start_utf8:part.end_utf8].decode() == part.content
        pieces.append(part.content)
        index, offset = selected.next_index, selected.next_offset
    assert "".join(pieces) == original.content and offset == 0


def test_irreducible_existing_contract_never_selects_or_skips_a_source(monkeypatch):
    monkeypatch.setattr(worker, "_prompt_previous", lambda items, **kwargs: items)
    previous = [{"item_key": "large", "text": "x" * 20_000}]
    original = worker._Msg(1, "user", "current source")
    with pytest.raises(worker.cc.CompilerRejection):
        batch([original], previous=previous)
    assert original.content == "current source"


@pytest.mark.parametrize("offset", [1, 2, 100])
def test_invalid_utf8_or_out_of_range_resume_offsets_are_rejected(offset):
    with pytest.raises(worker.cc.CompilerRejection):
        batch([worker._Msg(1, "user", "가나다🙂")], start_offset=offset)


@pytest.fixture
def fallback_env(request, monkeypatch):
    state, session = request.getfixturevalue("contract_environment")
    state["cap_writes"] = []
    async def persist(current, before, after):
        state["cap_writes"].append((copy.deepcopy(before),copy.deepcopy(after)))
        state["stored_payload"] = copy.deepcopy(after)
    monkeypatch.setattr(worker,"_persist_output_fallback",persist)
    return state, session


def retention_job(**changes):
    return replace(job({"retention_enabled":True,"memory_generation":0,"through_message_id":1}),**changes)


@pytest.mark.parametrize("attempt,maximum,remaining",[(1,3,2),(2,3,1)])
async def test_only_enlarged_length_uses_one_durable_small_planner_child(fallback_env,monkeypatch,attempt,maximum,remaining):
    state,session=fallback_env
    monkeypatch.setattr(llm,"generate",AsyncMock(return_value=llm.LLMResult("incomplete synthetic",1,1,finish_reason="length")))
    def forbidden(*args,**kwargs):
        pytest.fail("partial output cannot enter parse/merge/end supplementation")
    monkeypatch.setattr(worker.cc,"parse",forbidden)
    monkeypatch.setattr(worker,"merge_retained_items",forbidden)
    monkeypatch.setattr(worker.cc,"explicit_scope_end_candidates",forbidden)
    current=retention_job(attempt=attempt,max_attempts=maximum)
    result=await worker.handle_contract_compile(current)
    await result.apply_domain(session)
    child=state["queued"][-1]
    assert child["payload"]=={**current.payload,"adaptive_output_fallback":True,
                             "adaptive_output_attempts_remaining":remaining}
    assert child["max_attempts"]==remaining and state["stored_payload"]==child["payload"]
    assert state["cursor"]==0 and not state["published"] and not state["docs"]


async def test_no_fallback_child_when_the_failed_call_exhausted_its_attempts(fallback_env,monkeypatch):
    state,_=fallback_env
    monkeypatch.setattr(llm,"generate",AsyncMock(return_value=llm.LLMResult("partial",1,1,finish_reason="length")))
    with pytest.raises(JobRetry,match="contract_retention_length"):
        await worker.handle_contract_compile(retention_job(attempt=3,max_attempts=3))
    assert not state["queued"] and not state["cap_writes"] and state["cursor"]==0


@pytest.mark.parametrize("reason",["length","content_filter"])
async def test_small_length_or_refusal_never_starts_output_adaptation(fallback_env,monkeypatch,reason):
    state,_=fallback_env
    state["body"]="short source"
    monkeypatch.setattr(llm,"generate",AsyncMock(return_value=llm.LLMResult("partial",1,1,finish_reason=reason)))
    expected=JobRetry if reason=="length" else llm.IncompleteResponseError
    with pytest.raises(expected):
        await worker.handle_contract_compile(retention_job())
    assert not state["queued"] and not state["cap_writes"] and state["cursor"]==0


async def test_fallback_failure_off_on_new_row_does_not_refill_durable_cap(fallback_env,monkeypatch):
    state,_=fallback_env
    current=retention_job(max_attempts=2)
    current=replace(current,payload={**current.payload,"adaptive_output_fallback":True,
                                    "adaptive_output_attempts_remaining":2})
    model=AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(llm,"generate",model)
    with pytest.raises(TimeoutError):
        await worker.handle_contract_compile(current)
    assert state["stored_payload"]["adaptive_output_attempts_remaining"]==1
    # OFF cancels before inference. A later resume uses a fresh physical row/max3.
    state["enabled"]=False
    cancelled=replace(current,attempt=2,payload=copy.deepcopy(state["stored_payload"]))
    with pytest.raises(JobCancelled,match="disabled"):
        await worker.handle_contract_compile(cancelled)
    state["enabled"]=True
    resumed=replace(cancelled,id=uuid.uuid4(),attempt=1,max_attempts=3)
    model.side_effect=None
    model.return_value=llm.LLMResult("partial",1,1,finish_reason="length")
    with pytest.raises(JobRetry,match="contract_retention_length"):
        await worker.handle_contract_compile(resumed)
    assert state["stored_payload"]["adaptive_output_attempts_remaining"]==0
    exhausted=replace(resumed,id=uuid.uuid4(),payload=copy.deepcopy(state["stored_payload"]))
    with pytest.raises(JobFatal,match="attempts_exhausted"):
        await worker.handle_contract_compile(exhausted)
    assert model.await_count==2 and not state["queued"] and state["cursor"]==0


async def test_successful_small_parts_inherit_flag_and_unspent_failure_cap(fallback_env):
    state,session=fallback_env
    current=retention_job(max_attempts=2)
    current=replace(current,payload={**current.payload,"adaptive_output_fallback":True,
                                    "adaptive_output_attempts_remaining":2})
    result=await worker.handle_contract_compile(current)
    await result.apply_domain(session)
    child=state["queued"][-1]
    assert child["payload"]["adaptive_output_fallback"] is True
    assert child["payload"]["adaptive_output_attempts_remaining"]==2
    assert child["payload"]["partial_offset"]<=2048 and child["max_attempts"]==2
    assert state["stored_payload"]["adaptive_output_attempts_remaining"]==1


async def test_changed_source_restarts_fresh_without_reusing_partial_output_or_new_budget(fallback_env,monkeypatch):
    state,session=fallback_env
    monkeypatch.setattr(llm,"generate",AsyncMock(return_value=llm.LLMResult("partial",1,1,finish_reason="length")))
    result=await worker.handle_contract_compile(retention_job(attempt=2,max_attempts=3))
    state["body"]+=" Synthetic source change"
    await result.apply_domain(session)
    child=state["queued"][-1]
    assert child["payload"]["adaptive_output_fallback"] is True
    assert child["payload"]["adaptive_output_attempts_remaining"]==1 and child["max_attempts"]==1
    assert "partial_items" not in child["payload"] and state["cursor"]==0


async def test_first_length_then_off_preserves_marker_without_a_new_child(fallback_env,monkeypatch):
    state,session=fallback_env
    monkeypatch.setattr(llm,"generate",AsyncMock(return_value=llm.LLMResult("partial",1,1,finish_reason="length")))
    result=await worker.handle_contract_compile(retention_job())
    state["enabled"]=False
    await result.apply_domain(session)
    assert not state["queued"] and not state["docs"]
    assert state["stored_payload"]["adaptive_output_fallback"] is True
    assert state["stored_payload"]["adaptive_output_attempts_remaining"]==2


async def test_durable_cap_sql_is_same_job_lease_generation_and_privacy_fenced(monkeypatch):
    current=retention_job()
    captured=[]
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def execute(self,sql,params):
            captured.append((str(sql),params))
            return SimpleNamespace(first=lambda:(current.id,))
        async def commit(self): captured.append("commit")
    monkeypatch.setattr(worker,"get_sessionmaker",lambda:Session)
    monkeypatch.setattr(retention,"privacy_epoch_matches",AsyncMock(return_value=True))
    after={**current.payload,"adaptive_output_fallback":True,"adaptive_output_attempts_remaining":2}
    await worker._persist_output_fallback(current,current.payload,after)
    sql,params=captured[0]
    assert "j.lease_token=:token" in sql and "j.lease_until>now()" in sql
    assert "j.payload_redacted_at IS NULL" in sql and "c.memory_generation=:generation" in sql
    assert json.loads(params["payload"])==after and json.loads(params["before"])==current.payload
    assert params["payload_hash"]==hashlib.sha256(params["payload"].encode()).hexdigest()
    assert captured[-1]=="commit"
