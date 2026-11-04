"""대화 범위의 시작·종료 원문과 즉시 주입 중단. 모델/DB 호출 없음."""
import hashlib
import json
import uuid
from datetime import date
from types import SimpleNamespace

import pytest

from app.services import contract_compiler as compiler, contract_repo as repo
from app.services import interaction_contract as ic
from worker.contract_jobs import merge_retained_items
from worker import contract_jobs as worker


START = "이번 대화에서는 짧게 답해 줘"
END = "이제 평소대로 답해 줘"


def source(mid=1, body=START, **kwargs):
    return SimpleNamespace(id=mid, sender="user", content=body, activity_date=date(2026, 11, 2), **kwargs)


def item(scope="conversation"):
    kept, dropped = compiler.parse(json.dumps([{"kind": "response_style", "action": "prefer",
        "condition": "always", "target_literal": "짧게", "source_message_id": 1,
        "scope": scope, "evidence_quote": START}], ensure_ascii=False),
        user_message_ids={1}, source_messages=[source()])
    assert not dropped
    return merge_retained_items([], kept, generation=0)[0]


def end_candidate(previous, *, quote=END, mid=2, body=END):
    return compiler.parse(json.dumps([{"op": "end", "ends_item_key": previous["item_key"],
        "scope": "conversation", "source_message_id": mid, "evidence_quote": quote}], ensure_ascii=False),
        user_message_ids={mid}, source_messages=[source(mid, body)], existing_items=[previous])


@pytest.mark.parametrize("text", [None, "밥 뭐 먹을까", "굿모닝", "내일 보자", "날짜가 바뀌었네",
                                  "the anchor changed", "summary done", "おはよう"])
def test_topic_anchor_or_date_is_not_a_conversation_end(text):
    assert not compiler.conversation_scope_end_candidate(text)


@pytest.mark.parametrize("text", [END, "이번 대화는 여기까지 하자", "그 말투 그만 써",
    "Back to normal, please", "stop that style", "この会話はここまで", "元の口調に戻して"])
def test_explicit_scope_end_candidates_across_languages(text):
    assert compiler.conversation_scope_end_candidate(text)


def test_conversation_survives_new_day_and_retains_explicit_start():
    previous = item()
    assert previous["scope_start_message_id"] == previous["source_message_id"] == 1
    assert repo.active_retained_items([previous], activity_date=date(2027, 1, 1)) == [previous]


def test_end_preserves_original_body_start_and_new_original_evidence():
    previous = item()
    kept, dropped = end_candidate(previous)
    assert not dropped and len(kept) == 1
    result = merge_retained_items([previous], kept, generation=0)[0]
    assert result["target_literal"] == previous["target_literal"]
    assert result["source_hash"] == previous["source_hash"]
    assert result["scope_start_message_id"] == 1 and result["status"] == "ended"
    assert result["scope_end"] == {"source_message_id": 2,
        "source_hash": hashlib.sha256(END.encode()).hexdigest(), "start_utf8": 0, "end_utf8": len(END.encode())}
    assert repo.active_retained_items([result]) == []
    assert previous["status"] == "active"  # 게시 이력을 바꾸지 않는다.


@pytest.mark.parametrize("scope", ["persistent", "day"])
def test_conversation_end_cannot_close_other_scopes(scope):
    kept, dropped = end_candidate(item(scope))
    assert not kept and dropped


def test_end_needs_explicit_original_not_topic_change_or_fabricated_quote():
    previous = item()
    for quote, body in [("밥 뭐 먹을까", "밥 뭐 먹을까"), (END, "밥 뭐 먹을까")]:
        kept, dropped = end_candidate(previous, quote=quote, body=body)
        assert not kept and dropped
    kept, dropped = end_candidate(previous, mid=0)
    assert not kept and dropped


def test_model_cannot_invent_a_different_conversation_start():
    raw = [{"kind": "response_style", "action": "prefer", "condition": "always",
        "target_literal": "짧게", "source_message_id": 1, "scope": "conversation",
        "scope_start_message_id": 100, "evidence_quote": START}]
    kept, dropped = compiler.parse(json.dumps(raw), user_message_ids={1}, source_messages=[source()])
    assert not kept and dropped


def test_segment_preference_evidence_uses_original_offsets_and_whole_hash():
    prefix, quote = "🙂앞부분 ", START
    original = prefix + quote + " 끝"
    digest = hashlib.sha256(original.encode()).hexdigest()
    segment = source(4, quote, start_utf8=len(prefix.encode()),
                     end_utf8=len((prefix + quote).encode()), source_hash=digest)
    raw = [{"kind": "response_style", "action": "prefer", "condition": "always",
        "target_literal": "짧게", "source_message_id": 4, "scope": "conversation",
        "evidence_quote": quote}]
    kept, dropped = compiler.parse(json.dumps(raw), user_message_ids={4}, source_messages=[segment])
    assert not dropped
    value = kept[0]
    assert value.source_hash == digest
    assert original.encode()[value.start_utf8:value.end_utf8].decode() == quote


class Result:
    def __init__(self, rows):
        self.rows = rows
    def all(self):
        return self.rows


async def test_published_end_requires_both_normal_user_sources_and_intact_end_evidence():
    previous = item()
    kept, _ = end_candidate(previous)
    ended = merge_retained_items([previous], kept, generation=0)
    class Session:
        rows = [(1, START), (2, END)]
        async def execute(self, statement, params):
            assert statement is repo._VALID_SOURCES
            return Result(self.rows)
    session = Session()
    assert await repo.validate_retained_items(session, user_id=uuid.uuid4(), generation=0, items=ended)
    session.rows = [(1, START)]  # 종료 근거 삭제·safety 재분류도 같은 제외 결과다.
    with pytest.raises(ic.ContractViolation):
        await repo.validate_retained_items(session, user_id=uuid.uuid4(), generation=0, items=ended)
    session.rows = [(1, START), (2, END + "변경")]
    with pytest.raises(ic.ContractViolation):
        await repo.validate_retained_items(session, user_id=uuid.uuid4(), generation=0, items=ended)


@pytest.mark.parametrize("alter", [lambda value: value.update(scope_start_message_id=9),
    lambda value: value.update(status="ended"),
    lambda value: value.update(scope_end={"source_message_id": 2}),
    lambda value: value.update(scope="unlimited")])
def test_malformed_scope_metadata_is_rejected(alter):
    previous = item()
    alter(previous)
    with pytest.raises(ic.ContractViolation):
        repo._scope_metadata(previous)


async def test_current_explicit_end_suppresses_only_conversation_immediately(monkeypatch):
    conversation, persistent, today = item(), item("persistent"), item("day")
    persistent["target_literal"], today["target_literal"] = "평소 선호", "오늘 선호"
    async def retained(*args, **kwargs):
        return [conversation, persistent, today]
    monkeypatch.setattr(repo, "retained_document", retained)
    class ForbiddenSession:
        async def execute(self, *args, **kwargs):
            pytest.fail("현재 종료 발화를 다음 작업까지 기다릴 필요가 없다")
    result = await repo.published_text(ForbiddenSession(), user_id=uuid.uuid4(), locale="ko",
        retention_enabled=True, generation=0, activity_date=date(2026, 11, 2), current_text=END)
    assert "평소 선호" in result and "오늘 선호" in result and "짧게" not in result
    assert conversation["status"] == "active"  # 주입만 보류, 종료 상태는 worker가 근거 검증 후 게시.


async def test_unprocessed_end_is_found_beyond_first_two_hundred_messages(monkeypatch):
    conversation = item()
    async def retained(*args, **kwargs):
        return [conversation]
    monkeypatch.setattr(repo, "retained_document", retained)
    class Session:
        calls = 0
        async def execute(self, statement, params):
            self.calls += 1
            assert statement is repo._PENDING_CONVERSATION_ENDS
            after = params["after"]
            return Result([(mid, END if mid == 450 else "평범한 말")
                           for mid in range(after + 1, min(after + 201, 451))])
    session = Session()
    result = await repo.published_text(session, user_id=uuid.uuid4(), locale="ko",
        retention_enabled=True, generation=0, current_text="저녁 뭐 먹을까")
    assert result == "" and session.calls == 3


@pytest.mark.parametrize("body", [
    "이번 대화는 끝내지 말고 계속 짧게 답해 줘.",
    "Do not return to normal; keep this temporary style.",
    "この会話は終わらせないで、短く答えてね。",
    'The novel says "stop that style" but please keep our current style.',
    '"이번 대화는 여기까지 하자"는 소설 대사야.',
    "「いつもの口調に戻して」は台詞だよ。",
    "`stop that style`",
    "> stop that style",
])
def test_negated_or_quoted_end_preserves_scope_and_is_not_permanent_evidence(body):
    previous = item()
    assert not compiler.conversation_scope_end_candidate(body)
    assert not compiler.conversation_scope_end_applies(body, previous)
    assert compiler.explicit_scope_end_candidates([source(2, body)], [previous]) == []
    kept, dropped = end_candidate(previous, quote=body, body=body)
    assert not kept and dropped


def test_specific_style_end_cannot_close_address_and_old_end_cannot_close_new_start():
    style = item()
    address = {**style, "kind": "address", "action": "use", "target_literal": "친구",
               "item_key": "conversation:address:use:always:친구"}
    newer = {**style, "source_message_id": 3, "scope_start_message_id": 3,
             "item_key": "conversation:response_style:prefer:always:새로운요청"}
    assert compiler.conversation_scope_end_applies("그 말투 그만 써", style)
    assert not compiler.conversation_scope_end_applies("그 말투 그만 써", address)
    candidates = compiler.explicit_scope_end_candidates([source(2, "그 말투 그만 써")],
                                                       [style, address, newer])
    assert [candidate.ends_item_key for candidate in candidates] == [style["item_key"]]
    result = merge_retained_items([style, address, newer], candidates, generation=0)
    assert {entry["item_key"] for entry in result if entry["status"] == "active"} == {
        address["item_key"], newer["item_key"]}
    kept, dropped = end_candidate(address, quote="그 말투 그만 써", body="그 말투 그만 써")
    assert not kept and dropped


async def test_specific_style_end_immediately_preserves_conversation_address(monkeypatch):
    style = item()
    address = {**style, "kind": "address", "action": "use", "target_literal": "친구",
               "item_key": "conversation:address:use:always:친구"}
    async def retained(*args, **kwargs):
        return [style, address]
    monkeypatch.setattr(repo, "retained_document", retained)
    class Session:
        async def execute(self, statement, params):
            return Result([])
    rendered = await repo.published_text(Session(), user_id=uuid.uuid4(), locale="ko",
        retention_enabled=True, current_text="그 말투 그만 써")
    assert "친구" in rendered and "짧게" not in rendered


@pytest.mark.parametrize("body", ["이제 평소대로 답해 줘.", "Back to normal, please.", "いつもの口調に戻して。"])
def test_closed_affirmative_end_is_supplemented_without_model_output(body):
    previous = item()
    candidates = compiler.explicit_scope_end_candidates([source(2, body)], [previous])
    assert len(candidates) == 1
    result = merge_retained_items([previous], candidates, generation=0)[0]
    assert result["status"] == "ended" and result["scope_end"]["source_message_id"] == 2
    reference = result["scope_end"]
    assert body.encode()[reference["start_utf8"]:reference["end_utf8"]].decode()
    assert reference["source_hash"] == hashlib.sha256(body.encode()).hexdigest()


def test_expired_day_history_does_not_fill_new_contract_model_input():
    expired = item("day")
    previous = [{**expired, "item_key": f"day-style-{index}", "target_literal": f"old preference {index}",
                 "activity_date": "2025-01-01"} for index in range(180)]
    current = worker._Msg(2, "user", "새로운 대화 방식 요청", date(2026, 11, 2))
    assert worker._prompt_previous(previous, activity_date=current.activity_date) == []
    batch = worker.contract_batch([current], previous=previous, system=compiler.RETENTION_SYSTEM)
    assert len(batch.messages) == 1 and batch.next_index == 1
    assert len(previous) == 180 and all(entry["status"] == "active" for entry in previous)


def test_repeated_closed_end_keeps_target_and_uses_whole_unique_source_evidence():
    body = "그 말투 그만 써. 그 말투 그만 써."
    style = item()
    address = {**style, "kind": "address", "action": "use", "target_literal": "친구",
               "item_key": "conversation:address:use:always:친구"}
    candidates = compiler.explicit_scope_end_candidates([source(2, body)], [style, address])
    assert len(candidates) == 1 and candidates[0].ends_item_key == style["item_key"]
    candidate = candidates[0]
    assert candidate.start_utf8 == 0 and candidate.end_utf8 == len(body.encode())
    assert candidate.source_hash == hashlib.sha256(body.encode()).hexdigest()
    result = merge_retained_items([style, address], candidates, generation=0)
    assert next(entry for entry in result if entry["kind"] == "response_style")["status"] == "ended"
    assert next(entry for entry in result if entry["kind"] == "address")["status"] == "active"


def test_unprovable_explicit_end_fails_instead_of_advancing_coverage():
    body = "그 말투 그만 써. 그 말투 그만 써."
    invalid = source(2, body, start_utf8=3, end_utf8=999, source_hash="a" * 64)
    with pytest.raises(compiler.CompilerRejection, match="원문 구간"):
        compiler.explicit_scope_end_candidates([invalid], [item()])


@pytest.mark.parametrize("before,after,ended", [
    ("그 말투 그만 써. ", "", False),
    ("", ". 그 말투 그만 써", True),
    ("그 말투 그만 써. ", ". 그 말투 그만 써", True),
])
def test_same_message_start_and_end_follow_original_utf8_order(before, after, ended):
    body = before + START + after
    raw = [{"kind": "response_style", "action": "prefer", "condition": "always",
        "target_literal": "짧게", "source_message_id": 2, "scope": "conversation", "evidence_quote": START}]
    kept, dropped = compiler.parse(json.dumps(raw), user_message_ids={2}, source_messages=[source(2, body)])
    assert not dropped and len(kept) == 1
    prospective = merge_retained_items([], kept, generation=0)
    candidates = compiler.explicit_scope_end_candidates([source(2, body)], prospective)
    assert bool(candidates) is ended
    result = merge_retained_items(prospective, candidates, generation=0)[0]
    assert (result["status"] == "ended") is ended
    if ended:
        assert candidates[0].end_clause_start_utf8 >= prospective[0]["end_utf8"]
        # 반복된 앞/뒤 종료는 전체 원문 증명을 저장하되 실제 뒤 절로 순서를 판정한다.
        if before:
            assert candidates[0].start_utf8 == 0
            assert candidates[0].end_utf8 == len(body.encode())


def test_model_end_of_old_item_cannot_close_later_same_message_replacement():
    previous = item()
    body = "그 말투 그만 써. " + START
    raw = [{"op": "end", "ends_item_key": previous["item_key"], "scope": "conversation",
            "source_message_id": 2, "evidence_quote": "그 말투 그만 써"},
           {"kind": "response_style", "action": "prefer", "condition": "always",
            "target_literal": "짧게", "source_message_id": 2, "scope": "conversation", "evidence_quote": START}]
    kept, dropped = compiler.parse(json.dumps(raw), user_message_ids={2},
        source_messages=[source(2, body)], existing_items=[previous])
    assert len(kept) == 2 and not dropped
    kept.sort(key=lambda candidate: (candidate.source_message_id, candidate.operation == "end"))
    result = merge_retained_items([previous], kept, generation=0)[0]
    assert result["status"] == "active" and result["source_message_id"] == 2


async def test_saved_same_source_end_before_setter_is_rejected_on_read():
    body = "그 말투 그만 써. " + START
    digest = hashlib.sha256(body.encode()).hexdigest()
    previous = item()
    previous.update(source_message_id=2, scope_start_message_id=2, source_hash=digest,
        start_utf8=len("그 말투 그만 써. ".encode()), end_utf8=len(body.encode()), status="ended",
        scope_end={"source_message_id": 2, "source_hash": digest,
                   "start_utf8": 0, "end_utf8": len("그 말투 그만 써".encode())})
    class Session:
        async def execute(self, statement, params):
            return Result([(2, body)])
    with pytest.raises(ic.ContractViolation, match="순서"):
        await repo.validate_retained_items(Session(), user_id=uuid.uuid4(), generation=0, items=[previous])
