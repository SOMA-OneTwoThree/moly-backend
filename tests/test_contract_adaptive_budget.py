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
    state,session=fallback_env
    monkeypatch.setattr(llm,"generate",AsyncMock(return_value=llm.LLMResult("partial",1,1,finish_reason="length")))
    def forbidden(*args,**kwargs):
        pytest.fail("truncated output cannot be parsed")
    monkeypatch.setattr(worker.cc,"parse",forbidden)
    # 마지막 시도의 길이 초과는 대체 출력 작업도 dead도 아니다: 잘린 출력은 버리고 진행한다.
    result=await worker.handle_contract_compile(retention_job(attempt=3,max_attempts=3))
    assert result.result_code=="ok" and result.result_detail["discarded"]=={"length":0}
    assert not state["cap_writes"]
    await result.apply_domain(session)
    assert not any("output-fallback" in queued["dedup_key"] for queued in state["queued"])
    assert not state["published"]


@pytest.mark.parametrize("reason",["length","content_filter"])
async def test_small_length_or_refusal_never_starts_output_adaptation(fallback_env,monkeypatch,reason):
    state,_=fallback_env
    state["body"]="short source"
    monkeypatch.setattr(llm,"generate",AsyncMock(return_value=llm.LLMResult("partial",1,1,finish_reason=reason)))
    expected="contract_retention_length" if reason=="length" else "contract_retention_incomplete"
    with pytest.raises(JobRetry,match=expected):
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
    # 남은 대체 출력 호출이 1이면 이번이 마지막 호출이다. 재시도해도 다음 시도는 JobFatal이므로
    # 잘린 출력은 버리고 진행한다(dead 대신).
    result=await worker.handle_contract_compile(resumed)
    assert result.result_detail["discarded"]=={"length":0}
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


def _valid_and_unproven_candidates():
    valid = {"kind": "response_style", "action": "prefer", "condition": "always",
             "target_literal": "short replies", "source_message_id": 1,
             "scope": "conversation", "evidence_quote": "use short replies"}
    unproven = {**valid, "target_literal": "long replies", "evidence_quote": "never said in the source"}
    return llm.LLMResult(json.dumps([valid, unproven]), 5, 3, finish_reason="stop")


async def test_unproven_item_retries_while_attempts_remain(fallback_env, monkeypatch):
    state, _ = fallback_env
    state["body"] = "This conversation, use short replies."
    monkeypatch.setattr(llm, "generate", AsyncMock(return_value=_valid_and_unproven_candidates()))
    with pytest.raises(JobRetry, match="contract_retention_evidence"):
        await worker.handle_contract_compile(retention_job(attempt=2, max_attempts=3))
    assert state["cursor"] == 0 and not state["published"]


async def test_last_attempt_keeps_only_proven_items_and_advances_instead_of_dying(fallback_env, monkeypatch):
    state, session = fallback_env
    state["body"] = "This conversation, use short replies."
    monkeypatch.setattr(llm, "generate", AsyncMock(return_value=_valid_and_unproven_candidates()))
    result = await worker.handle_contract_compile(retention_job(attempt=3, max_attempts=3))
    await result.apply_domain(session)
    assert state["cursor"] == 1
    literals = json.dumps(state["published"], ensure_ascii=False)
    assert "short replies" in literals and "long replies" not in literals


# ── 마지막 시도의 내용 실패는 dead 대신 진행한다 ─────────────────────────

_SHORT = "This conversation, use short replies."


def _model_output(kind):
    if kind == "schema":
        return llm.LLMResult("not a json array", 5, 3, finish_reason="stop")
    if kind == "incomplete":
        return llm.LLMResult(None, 5, 3, finish_reason="content_filter")
    if kind == "length":
        return llm.LLMResult('[{"kind":', 5, 3, finish_reason="length")
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["schema", "incomplete", "length"])
async def test_content_failure_retries_while_attempts_remain(fallback_env, monkeypatch, kind):
    state, _ = fallback_env
    state["body"] = _SHORT
    monkeypatch.setattr(llm, "generate", AsyncMock(return_value=_model_output(kind)))
    with pytest.raises(JobRetry, match=f"contract_retention_{kind}"):
        await worker.handle_contract_compile(retention_job(attempt=2, max_attempts=3))
    assert state["cursor"] == 0 and not state["queued"]


@pytest.mark.parametrize("kind", ["schema", "incomplete", "length"])
async def test_last_attempt_content_failure_advances_with_nothing_kept(fallback_env, monkeypatch, kind):
    state, session = fallback_env
    state["body"] = _SHORT
    monkeypatch.setattr(llm, "generate", AsyncMock(return_value=_model_output(kind)))
    if kind != "schema":
        def forbidden(*args, **kwargs):
            pytest.fail("incomplete or truncated output must not be parsed")
        monkeypatch.setattr(worker.cc, "parse", forbidden)
    result = await worker.handle_contract_compile(retention_job(attempt=3, max_attempts=3))
    assert result.result_code == "ok"
    assert result.result_detail["discarded"] == {kind: 0}
    assert result.result_detail["kept"] == 0 and result.result_detail["document_kept"] is False
    await result.apply_domain(session)
    assert state["cursor"] == 1 and not state["published"] and not state["queued"]


def _fail_merge_with_model_candidates(monkeypatch, *, always=False):
    original = worker.merge_retained_items

    def merge(previous, candidates, *, generation):
        if always or candidates:
            raise worker.cc.CompilerRejection("synthetic merge rejection")
        return original(previous, candidates, generation=generation)
    monkeypatch.setattr(worker, "merge_retained_items", merge)


async def test_merge_failure_retries_while_attempts_remain(fallback_env, monkeypatch):
    state, _ = fallback_env
    state["body"] = _SHORT
    _fail_merge_with_model_candidates(monkeypatch)
    with pytest.raises(JobRetry, match="contract_retention_merge"):
        await worker.handle_contract_compile(retention_job(attempt=2, max_attempts=3))
    assert state["cursor"] == 0


async def test_last_attempt_merge_failure_drops_model_candidates_and_advances(fallback_env, monkeypatch):
    state, session = fallback_env
    state["body"] = _SHORT
    _fail_merge_with_model_candidates(monkeypatch)
    result = await worker.handle_contract_compile(retention_job(attempt=3, max_attempts=3))
    assert result.result_detail["discarded"] == {"merge": 1}
    assert result.result_detail["document_kept"] is False
    await result.apply_domain(session)
    assert state["cursor"] == 1 and not state["published"]
    assert state["calls"] == 1  # 모델을 다시 부르지 않는다.


async def test_last_attempt_budget_overflow_drops_model_candidates_and_advances(fallback_env, monkeypatch):
    state, session = fallback_env
    state["body"] = _SHORT
    original = worker.ic.render_document
    monkeypatch.setattr(worker.ic, "render_document",
                        lambda directives, **kwargs: "x" * 3000 if directives else original(directives, **kwargs))
    with pytest.raises(JobRetry, match="contract_retention_budget"):
        await worker.handle_contract_compile(retention_job(attempt=1, max_attempts=3))
    result = await worker.handle_contract_compile(retention_job(attempt=3, max_attempts=3))
    assert result.result_detail["discarded"] == {"budget": 1}
    await result.apply_domain(session)
    assert state["cursor"] == 1 and not state["published"]


async def test_last_attempt_keeps_document_and_only_advances_when_recompute_also_fails(fallback_env, monkeypatch):
    state, session = fallback_env
    original = worker._Msg(1, "user", _SHORT)
    candidate = {"kind": "response_style", "action": "prefer", "condition": "always",
                 "target_literal": "short replies", "source_message_id": 1,
                 "scope": "conversation", "evidence_quote": "use short replies"}
    kept, dropped = worker.cc.parse(json.dumps([candidate]), user_message_ids={1}, source_messages=[original])
    assert not dropped
    existing = worker.merge_retained_items([], kept, generation=0)
    state["published"] = copy.deepcopy(existing)
    state.update(body="another synthetic message", cursor=1, message_id=2)
    _fail_merge_with_model_candidates(monkeypatch, always=True)
    result = await worker.handle_contract_compile(replace(retention_job(attempt=3, max_attempts=3),
        payload={"retention_enabled": True, "memory_generation": 0, "through_message_id": 2,
                 "privacy_epoch": 0}))
    assert result.result_detail["discarded"] == {"merge": 0}
    assert result.result_detail["document_kept"] is True
    await result.apply_domain(session)
    assert state["cursor"] == 2 and state["published"] == existing


@pytest.mark.parametrize("remaining,dies", [(1, False), (2, True)])
async def test_adaptive_output_mode_counts_its_remaining_model_calls_as_the_last_attempt(
        fallback_env, monkeypatch, remaining, dies):
    state, _ = fallback_env
    state["body"] = _SHORT
    monkeypatch.setattr(llm, "generate", AsyncMock(return_value=_model_output("schema")))
    current = retention_job(attempt=1, max_attempts=3)
    current = replace(current, payload={**current.payload, "adaptive_output_fallback": True,
                                        "adaptive_output_attempts_remaining": remaining})
    if dies:
        with pytest.raises(JobRetry, match="contract_retention_schema"):
            await worker.handle_contract_compile(current)
    else:
        result = await worker.handle_contract_compile(current)
        assert result.result_detail["discarded"] == {"schema": 0}
    assert state["stored_payload"]["adaptive_output_attempts_remaining"] == remaining - 1


async def test_input_budget_failure_has_its_own_code(fallback_env, monkeypatch):
    state, _ = fallback_env
    state["body"] = _SHORT

    def too_large(*args, **kwargs):
        raise retention.RetentionError("context_input_budget")
    # 원문 선택은 통과했는데 모델 호출 직전 검사만 실패하는 경우(결정적 실패).
    monkeypatch.setattr(worker.context_retention, "fits_input_budget", lambda *args, **kwargs: True)
    monkeypatch.setattr(worker.context_retention, "assert_input_budget", too_large)
    with pytest.raises(JobRetry, match="contract_retention_input_budget"):
        await worker.handle_contract_compile(retention_job(attempt=3, max_attempts=3))


# ── dead 뒤 다시 만든 작업의 후속 작업 ─────────────────────────────────

_REDO = {"redo_of": "00000000-0000-0000-0000-0000000002bc", "redo_depth": 2}


async def test_redo_partial_followup_key_does_not_collide_with_the_dead_chain(fallback_env):
    state, session = fallback_env
    current = retention_job()
    result = await worker.handle_contract_compile(replace(current, payload={**current.payload, **_REDO}))
    await result.apply_domain(session)
    queued = state["queued"][-1]
    assert queued["payload"]["partial_offset"] > 0
    assert queued["dedup_key"].endswith(f":redo:{_REDO['redo_of']}")
    assert queued["payload"]["redo_of"] == _REDO["redo_of"] and queued["payload"]["redo_depth"] == 2
    plain = await worker.handle_contract_compile(current)
    await plain.apply_domain(session)
    assert ":redo:" not in state["queued"][-1]["dedup_key"]


async def test_redo_boundary_followup_key_does_not_collide_with_the_dead_chain(fallback_env, monkeypatch):
    state, session = fallback_env
    state["body"] = _SHORT
    monkeypatch.setattr(worker, "_MAX_SOURCE", 1)
    current = retention_job()
    payload = {**current.payload, "through_message_id": 5, **_REDO}
    result = await worker.handle_contract_compile(replace(current, payload=payload))
    await result.apply_domain(session)
    queued = state["queued"][-1]
    assert state["cursor"] == 1
    assert queued["dedup_key"] == f"contract-retention:{current.user_id}:0:1:5:redo:{_REDO['redo_of']}"
    assert queued["payload"]["after_message_id"] == 1 and queued["payload"]["redo_depth"] == 2


async def test_restart_current_carries_cursor_and_redo_marker(fallback_env):
    state, session = fallback_env
    current = retention_job()
    result = await worker.handle_contract_compile(replace(current, payload={**current.payload, **_REDO}))
    state["body"] += " synthetic source change"
    await result.apply_domain(session)
    payload = state["queued"][-1]["payload"]
    assert payload == {"retention_enabled": True, "memory_generation": 0, "through_message_id": 1,
                       "after_message_id": 0, "privacy_epoch": 0, **_REDO}
