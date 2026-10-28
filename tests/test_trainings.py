"""트레이닝 서비스 흐름 — 메모리 저장소로 DB 읽기·쓰기 함수를 바꿔 끼운다.

주관식 제출은 트랜잭션 1(읽기) → AI 채점 → 트랜잭션 2(잠금 아래 재확인·쓰기)다. AI 호출 중에
저장소를 바꿔 그사이 다른 제출이 끼어든 경우를 흉내 낸다.
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.errors import AppError
from app.models.training import (
    TrainingAnswer,
    TrainingClaim,
    TrainingProgress,
    TrainingReward,
    TrainingTries,
    TrainingSubmission,
)
from app.schemas.trainings import TrainingAnswerRequest, TrainingClaimRequest
from app.services import entitlement, gating, hay_ledger, llm, privacy, training_catalog, trainings
from app.services.training_catalog import TrainingCatalog

CATALOG = TrainingCatalog.load(Path(__file__).parent / "fixtures/trainings/catalog.json")
TID = "practice-basics"
UID = uuid.UUID("00000000-0000-4000-8000-000000000001")
OTHER = uuid.UUID("00000000-0000-4000-8000-000000000002")
USER = str(UID)
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)


class Store:
    def __init__(self) -> None:
        self.submissions = {}
        self.progress: dict[tuple, TrainingProgress] = {}
        self.answers: dict[tuple, TrainingAnswer] = {}
        self.tries: dict[uuid.UUID, TrainingTries] = {}
        self.rewards: dict[tuple, TrainingReward] = {}
        self.claims: list[TrainingClaim] = []
        self.balance = 100
        self.ledger: list[tuple] = []
        self.events: list[str] = []
        self.ai_replies: list[str] = []
        self.during_ai = None  # AI 채점 중에 실행할 끼어들기
        self.language = "ja-JP"
        # 기존 흐름 검사는 유효한 구독 계정으로 실행하고, 권한 검사는 각 테스트에서 바꾼다.
        self.entitlement = {"plan": "monthly"}
        self.entitlement_calls: list[tuple[str, datetime]] = []


class FakeSession:
    def __init__(self, store: Store) -> None:
        self.store = store

    def add(self, row) -> None:
        s = self.store
        if isinstance(row, TrainingSubmission):
            s.submissions[(row.user_id, row.training_id, row.request_id)] = row
        elif isinstance(row, TrainingProgress):
            s.progress[(row.user_id, row.training_id)] = row
        elif isinstance(row, TrainingAnswer):
            s.answers[(row.user_id, row.training_id, row.stage)] = row
        elif isinstance(row, TrainingTries):
            s.tries[row.user_id] = row
        elif isinstance(row, TrainingReward):
            s.rewards[(row.user_id, row.training_id, row.stage)] = row
        elif isinstance(row, TrainingClaim):
            s.claims.append(row)
        else:  # pragma: no cover - 서비스가 다른 행을 넣으면 테스트가 알아야 한다
            raise AssertionError(f"unexpected row {row!r}")

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.store.events.append("commit")


@pytest.fixture
def store(monkeypatch) -> Store:
    s = Store()

    async def load_submission(session, uid, training_id, request_id):
        return s.submissions.get((uid, training_id, request_id))

    async def load_progress(session, uid, training_id):
        return s.progress.get((uid, training_id))

    async def load_all_progress(session, uid):
        return {tid: row for (owner, tid), row in s.progress.items() if owner == uid}

    async def load_answers(session, uid, training_id):
        rows = [row for (owner, tid, _), row in s.answers.items() if (owner, tid) == (uid, training_id)]
        return sorted(rows, key=lambda row: row.stage)

    async def load_answer(session, uid, training_id, stage):
        return s.answers.get((uid, training_id, stage))

    async def load_tries(session, uid):
        return s.tries.get(uid)

    async def reward_granted(session, uid, training_id, stage):
        return (uid, training_id, stage) in s.rewards

    async def lock(session, uid):
        s.events.append("lock")

    async def ensure(session, uid):
        s.events.append("ensure")

    async def account_language(session, uid):
        return s.language

    async def resolve_entitlement(session, user_id, now):
        s.entitlement_calls.append((user_id, now))
        return s.entitlement

    async def apply(session, uid, tx_type, amount, **kwargs):
        s.balance += amount
        s.ledger.append((uid, tx_type, amount))
        return SimpleNamespace(balance_after=s.balance)

    async def generate(system, convo, **kwargs):
        s.events.append("ai")
        if s.during_ai is not None:
            s.during_ai()
        reply = s.ai_replies.pop(0) if s.ai_replies else '{"pass": true, "hint": ""}'
        return llm.LLMResult(text=reply, input_tokens=1, output_tokens=1)

    monkeypatch.setattr(training_catalog, "current", lambda: CATALOG)
    monkeypatch.setattr(trainings, "_load_submission", load_submission)
    monkeypatch.setattr(trainings, "_load_progress", load_progress)
    monkeypatch.setattr(trainings, "_load_all_progress", load_all_progress)
    monkeypatch.setattr(trainings, "_load_answers", load_answers)
    monkeypatch.setattr(trainings, "_load_answer", load_answer)
    monkeypatch.setattr(trainings, "_load_tries", load_tries)
    monkeypatch.setattr(trainings, "_reward_granted", reward_granted)
    monkeypatch.setattr(trainings, "advisory_xact_lock", lock)
    monkeypatch.setattr(privacy, "ensure_subject_active", ensure)
    monkeypatch.setattr(trainings, "_account_language", account_language)
    monkeypatch.setattr(trainings, "_now", lambda: NOW)
    monkeypatch.setattr(gating, "resolve_entitlement", resolve_entitlement)
    monkeypatch.setattr(hay_ledger, "apply", apply)
    monkeypatch.setattr(llm, "generate", generate)
    return s


@pytest.fixture
def session(store) -> FakeSession:
    return FakeSession(store)


def _pick(option_id: str, request_id: uuid.UUID | None = None) -> TrainingAnswerRequest:
    return TrainingAnswerRequest(client_request_id=request_id or uuid.uuid4(), option_id=option_id)


def _write(text: str, request_id: uuid.UUID | None = None) -> TrainingAnswerRequest:
    return TrainingAnswerRequest(client_request_id=request_id or uuid.uuid4(), text=text)


async def _submit(session, stage, body, *, locale="ko", now=NOW, training_id=TID):
    return await trainings.submit(session, USER, training_id, stage, body, locale=locale, now=now)


async def _pass_up_to(session, last_stage: int) -> None:
    answers = {1: _pick("b"), 2: _write("포장 실수"), 3: _pick("c"), 4: _pick("a"), 5: _write("나사를 다시 조였다")}
    for stage in range(1, last_stage + 1):
        result = await _submit(session, stage, answers.get(stage) or _write("끝까지 해낸 내가 고맙다"))
        assert result["result"] == "passed", stage


# --- 목록·상세 ---


async def test_empty_catalog_lists_nothing_without_touching_the_database(monkeypatch):
    monkeypatch.setattr(training_catalog, "current", lambda: training_catalog.EMPTY)
    assert await trainings.list_trainings(None, USER, locale="ko") == {"trainings": []}


async def test_cards_follow_display_order_and_my_progress_only(store, session):
    store.progress[(OTHER, TID)] = trainings._new_progress(OTHER, TID, NOW)
    store.progress[(OTHER, TID)].next_stage = 5
    await _pass_up_to(session, 2)
    cards = (await trainings.list_trainings(session, USER, locale="en"))["trainings"]
    assert [c["id"] for c in cards] == ["mini", TID]
    assert cards[1] == {
        "id": TID, "title": "Practice basics", "icon_key": "mending_heart",
        "stage_count": 6, "passed_count": 2, "completed": False,
    }
    assert cards[0]["passed_count"] == 0


async def test_missing_locale_header_uses_the_account_language(store, session):
    cards = (await trainings.list_trainings(session, USER, locale=None))["trainings"]
    assert cards[1]["title"] == "練習の基本"
    store.language = "zh-Hant-TW"
    cards = (await trainings.list_trainings(session, USER, locale=None))["trainings"]
    assert cards[1]["title"] == "Practice basics"


async def test_detail_for_a_new_user(store, session):
    detail = await trainings.get_training(session, USER, TID, locale="ko", now=NOW)
    assert detail["progress"] == {"next_stage": 1, "passed_count": 0, "completed": False}
    assert detail["answers"] == []
    assert detail["tries"] == {"left": 5, "max": 5, "next_refill_at": None, "full_at": None}
    assert [s["number"] for s in detail["stages"]] == [1, 2, 3, 4, 5, 6]
    assert "correct" not in str(detail["stages"])


async def test_unknown_training_or_stage_is_not_found(store, session):
    with pytest.raises(AppError) as missing:
        await trainings.get_training(session, USER, "nope", locale="ko")
    assert (missing.value.code, missing.value.http_status) == ("TRAINING_NOT_FOUND", 404)
    with pytest.raises(AppError) as stage:
        await _submit(session, 7, _pick("a"))
    assert stage.value.code == "TRAINING_NOT_FOUND"


# --- 객관식 ---


async def test_correct_pick_passes_and_opens_the_next_stage(store, session):
    request = _pick("b")
    result = await _submit(session, 1, request)
    assert result["result"] == "passed" and result["judge"] == "answer_key"
    assert result["explanation"] == CATALOG.get(TID).stage(1).explanation.ko
    assert result["hint"] is None and result["reward"] is None
    assert result["answer"] == {"stage": 1, "option_id": "b", "text": None, "passed_at": NOW}
    assert result["progress"] == {"next_stage": 2, "passed_count": 1, "completed": False}
    assert result["tries"]["left"] == 5
    progress = store.progress[(UID, TID)]
    assert progress.last_request_id == request.client_request_id
    assert store.events == ["lock", "ensure", "commit"]


async def test_wrong_pick_spends_a_try_and_saves_no_answer(store, session):
    result = await _submit(session, 1, _pick("a"), locale="ja")
    assert result["result"] == "retry"
    assert result["hint"] == CATALOG.get(TID).stage(1).options[0].hint.ja
    assert result["answer"] is None and result["explanation"] is None
    assert result["tries"] == {
        "left": 4, "max": 5, "next_refill_at": NOW + timedelta(hours=1), "full_at": NOW + timedelta(hours=1),
    }
    assert store.answers == {}
    assert store.progress[(UID, TID)].next_stage == 1
    assert (store.tries[UID].tries_left, store.tries[UID].refill_from) == (4, NOW)
    # 통과하지 못한 답의 내용은 저장하지 않는다(판정·힌트만).
    assert store.progress[(UID, TID)].last_result == {
        "stage": 1, "result": "retry", "judge": "answer_key", "hint": result["hint"],
        "explanation": None, "reward": None,
    }


async def test_resending_the_same_request_replays_without_spending_again(store, session):
    retry = _pick("a")
    first = await _submit(session, 1, retry)
    again = await _submit(session, 1, retry, now=NOW + timedelta(minutes=5))
    assert again == first
    assert store.tries[UID].tries_left == 4


    passed = _pick("b")
    first = await _submit(session, 1, passed)
    again = await _submit(session, 1, passed)
    assert again == first and again["progress"]["next_stage"] == 2
    assert len(store.answers) == 1


async def test_dev_exhausted_account_can_repeat_wrong_pick_and_write(store, session, monkeypatch):
    monkeypatch.setattr(trainings.settings, 'environment', 'development')
    store.tries[UID] = TrainingTries(user_id=UID, tries_left=0, refill_from=NOW, updated_at=NOW)
    detail = await trainings.get_training(session, str(UID), TID, locale='ko', now=NOW)
    assert detail['tries']['left'] == 5
    for _ in range(7):
        result = await _submit(session, 1, _pick('a'))
        assert (result['result'], result['tries']['left']) == ('retry', 5)
        assert result['answer'] is None and result['reward'] is None
    await _submit(session, 1, _pick('b'))
    for _ in range(7):
        result = await _submit(session, 2, TrainingAnswerRequest(client_request_id=uuid.uuid4(), text='ㅋㅋㅋ'))
        assert (result['result'], result['tries']['left']) == ('retry', 5)
    assert store.tries[UID].tries_left == 0  # QA does not overwrite the normal balance.


@pytest.mark.parametrize('environment', ['production', 'prod', 'local', ''])
async def test_non_dev_still_deducts_and_blocks_exhausted_account(store, session, monkeypatch, environment):
    monkeypatch.setattr(trainings.settings, 'environment', environment)
    result = await _submit(session, 1, _pick('a'))
    assert result['tries']['left'] == 4
    store.tries[UID].tries_left = 0
    with pytest.raises(AppError) as error:
        await _submit(session, 1, _pick('b'))
    assert error.value.code == 'TRAINING_NO_TRIES'


async def test_stage_must_be_the_current_one(store, session):
    with pytest.raises(AppError) as locked:
        await _submit(session, 2, _write("포장 실수"))
    assert (locked.value.code, locked.value.http_status) == ("TRAINING_STAGE_NOT_CURRENT", 409)
    await _submit(session, 1, _pick("b"))
    with pytest.raises(AppError) as passed:
        await _submit(session, 1, _pick("b"))
    assert passed.value.code == "TRAINING_STAGE_NOT_CURRENT"


async def test_no_tries_blocks_every_answer_even_a_correct_pick(store, session):
    store.tries[UID] = TrainingTries(user_id=UID, tries_left=0, refill_from=NOW - timedelta(minutes=10), updated_at=NOW)
    with pytest.raises(AppError) as blocked:
        await _submit(session, 1, _pick("b"))
    assert (blocked.value.code, blocked.value.http_status) == ("TRAINING_NO_TRIES", 409)
    assert blocked.value.details == {"tries": {
        "left": 0, "max": 5,
        "next_refill_at": NOW + timedelta(minutes=50), "full_at": NOW + timedelta(hours=4, minutes=50),
    }}
    assert store.answers == {}
    # 한 시간이 지나 하나가 차면 다시 낼 수 있다.
    later = await _submit(session, 1, _pick("b"), now=NOW + timedelta(minutes=50))
    assert later["result"] == "passed" and later["tries"]["left"] == 1


@pytest.mark.parametrize(
    ("stage", "body"),
    [(1, {"text": "글"}), (1, {"option_id": "z"}), (2, {"option_id": "a"})],
)
async def test_answer_must_match_the_stage(store, session, stage, body):
    if stage == 2:
        await _submit(session, 1, _pick("b"))
    request = TrainingAnswerRequest(client_request_id=uuid.uuid4(), **body)
    with pytest.raises(AppError) as invalid:
        await _submit(session, stage, request)
    assert (invalid.value.code, invalid.value.http_status) == ("VALIDATION", 422)


# --- 주관식 ---


async def test_write_is_graded_outside_the_transaction(store, session):
    await _submit(session, 1, _pick("b"))
    store.events.clear()
    result = await _submit(session, 2, _write("포장 실수"))
    assert result["result"] == "passed" and result["judge"] == "ai"
    assert result["answer"]["text"] == "포장 실수"
    assert store.events == ["lock", "ensure", "commit", "ai", "lock", "ensure", "commit"]


async def test_rule_failure_spends_a_try_without_ai(store, session):
    await _submit(session, 1, _pick("b"))
    store.events.clear()
    result = await _submit(session, 2, _write("못"), locale="en")
    assert (result["result"], result["judge"]) == ("retry", "rule")
    assert result["hint"] == "Could you write a little more?"
    assert "ai" not in store.events
    assert store.tries[UID].tries_left == 4


async def test_ai_failure_passes_the_answer(store, session, monkeypatch):
    await _submit(session, 1, _pick("b"))

    async def down(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(llm, "generate", down)
    result = await _submit(session, 2, _write("포장 실수"))
    assert (result["result"], result["judge"]) == ("passed", "fallback")


async def test_no_tries_stops_before_calling_ai(store, session):
    await _submit(session, 1, _pick("b"))
    store.tries[UID] = TrainingTries(user_id=UID, tries_left=0, refill_from=NOW, updated_at=NOW)
    store.events.clear()
    with pytest.raises(AppError) as blocked:
        await _submit(session, 2, _write("포장 실수"))
    assert blocked.value.code == "TRAINING_NO_TRIES"
    assert "ai" not in store.events


async def test_last_try_used_meanwhile_rejects_a_failed_answer(store, session):
    await _submit(session, 1, _pick("b"))
    store.tries[UID] = TrainingTries(user_id=UID, tries_left=1, refill_from=NOW, updated_at=NOW)
    store.ai_replies.append('{"pass": false, "hint": "다른 까닭을 떠올려 볼까요?"}')

    def used_elsewhere():
        store.tries[UID].tries_left = 0

    store.during_ai = used_elsewhere
    with pytest.raises(AppError) as blocked:
        await _submit(session, 2, _write("내 손재주가 없어서"))
    assert blocked.value.code == "TRAINING_NO_TRIES"
    assert store.tries[UID].tries_left == 0


async def test_last_try_used_meanwhile_still_accepts_a_pass(store, session):
    await _submit(session, 1, _pick("b"))
    store.tries[UID] = TrainingTries(user_id=UID, tries_left=1, refill_from=NOW, updated_at=NOW)

    def used_elsewhere():
        store.tries[UID].tries_left = 0

    store.during_ai = used_elsewhere
    result = await _submit(session, 2, _write("포장 실수"))
    assert result["result"] == "passed" and result["tries"]["left"] == 0


async def test_resent_write_is_replayed_without_ai(store, session):
    await _submit(session, 1, _pick("b"))
    request = _write("내 손재주가 없어서")
    store.ai_replies.append('{"pass": false, "hint": "다른 까닭을 떠올려 볼까요?"}')
    first = await _submit(session, 2, request)
    store.events.clear()
    again = await _submit(session, 2, request)
    assert again == first and first["result"] == "retry"
    assert store.tries[UID].tries_left == 4
    assert "ai" not in store.events


async def test_request_recorded_during_ai_is_returned_from_transaction_two(store, session):
    await _submit(session, 1, _pick("b"))
    request = _write("포장 실수")
    saved = {"stage": 2, "result": "retry", "judge": "ai", "hint": "먼저 끝난 판정", "explanation": None, "reward": None}

    def twin_finished():
        progress = store.progress[(UID, TID)]
        progress.last_request_id = request.client_request_id
        progress.last_result = saved
        store.submissions[(UID, TID, request.client_request_id)].result = saved

    store.during_ai = twin_finished
    store.ai_replies.append('{"pass": false, "hint": "이번 판정"}')
    result = await _submit(session, 2, request)
    assert result["hint"] == "먼저 끝난 판정"
    assert UID not in store.tries  # 두 번째로 끝난 쪽은 기회를 쓰지 않는다


async def test_stage_passed_meanwhile_is_not_current(store, session):
    await _submit(session, 1, _pick("b"))

    def passed_elsewhere():
        store.progress[(UID, TID)].next_stage = 3

    store.during_ai = passed_elsewhere
    with pytest.raises(AppError) as moved:
        await _submit(session, 2, _write("포장 실수"))
    assert moved.value.code == "TRAINING_STAGE_NOT_CURRENT"


# --- 돌아보기 보상·완료 ---


async def test_review_points_pay_once_and_the_last_stage_completes(store, session):
    await _pass_up_to(session, 2)
    review = await _submit(session, 3, _pick("c"))
    assert review["reward"] == {"granted": 15, "balance_after": 115}
    assert store.ledger == [(UID, "training_reward", 15)]
    await _pass_up_to_from(session, 4)
    progress = store.progress[(UID, TID)]
    assert progress.next_stage is None and progress.completed_at == NOW
    assert [entry[2] for entry in store.ledger] == [15, 40]


async def _pass_up_to_from(session, first_stage: int) -> None:
    answers = {4: _pick("a"), 5: _write("나사를 다시 조였다"), 6: _write("끝까지 해낸 내가 고맙다")}
    for stage in range(first_stage, 7):
        result = await _submit(session, stage, answers[stage])
        assert result["result"] == "passed", stage
    assert result["progress"] == {"next_stage": None, "passed_count": 6, "completed": True}
    assert result["reward"] == {"granted": 40, "balance_after": 155}


async def test_other_users_progress_is_not_mixed_in(store, session):
    store.progress[(OTHER, TID)] = trainings._new_progress(OTHER, TID, NOW)
    store.progress[(OTHER, TID)].next_stage = 4
    store.answers[(OTHER, TID, 1)] = TrainingAnswer(user_id=OTHER, training_id=TID, stage=1, option_id="b", passed_at=NOW)
    store.tries[OTHER] = TrainingTries(user_id=OTHER, tries_left=0, refill_from=NOW, updated_at=NOW)
    detail = await trainings.get_training(session, USER, TID, locale="ko", now=NOW)
    assert detail["progress"]["next_stage"] == 1 and detail["answers"] == [] and detail["tries"]["left"] == 5


# --- 채점 이의 ---


async def test_claims_are_saved_for_all_stage_types(store, session):
    body = TrainingClaimRequest(reason="should_pass", answer_text=" 포장 실수 ", verdict="retry", hint="다시 써 볼까요?")
    await trainings.claim(session, USER, TID, 2, body)
    claim = store.claims[0]
    assert (claim.user_id, claim.stage, claim.reason, claim.answer_text, claim.verdict) == (
        UID, 2, "should_pass", "포장 실수", "retry",
    )
    await trainings.claim(session, USER, TID, 1, body)
    assert store.claims[-1].stage == 1
    with pytest.raises(AppError) as missing:
        await trainings.claim(session, USER, TID, 9, body)
    assert missing.value.code == "TRAINING_NOT_FOUND"


async def test_replay_survives_intervening_failed_requests(store, session):
    first_request = _pick('a')
    first = await _submit(session, 1, first_request)
    await _submit(session, 1, _pick('c'))
    again = await _submit(session, 1, first_request)
    assert again['result'] == first['result'] == 'retry'
    assert again['hint'] == first['hint']
    assert again['tries']['left'] == 3
    assert store.tries[UID].tries_left == 3


async def test_same_request_id_cannot_change_payload_or_stage(store, session):
    request = _pick('a')
    await _submit(session, 1, request)
    with pytest.raises(AppError) as changed:
        await _submit(session, 1, _pick('b', request.client_request_id))
    assert changed.value.http_status == 422
    await _submit(session, 1, _pick('b'))
    with pytest.raises(AppError) as moved:
        await _submit(session, 2, _write('포장 실수', request.client_request_id))
    assert moved.value.http_status == 422
    assert store.tries[UID].tries_left == 4


async def test_concurrent_retries_reserve_one_ai_call(store, session, monkeypatch):
    import asyncio
    await _submit(session, 1, _pick('b'))
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def grade(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await finish.wait()
        return llm.LLMResult(text='{"pass": false, "hint": "다른 이유는요?"}', input_tokens=1, output_tokens=1)

    monkeypatch.setattr(llm, 'generate', grade)
    request = _write('내가 못해서')
    first = asyncio.create_task(_submit(FakeSession(store), 2, request))
    await entered.wait()
    rest = await asyncio.gather(*[_submit(FakeSession(store), 2, request) for _ in range(7)], return_exceptions=True)
    assert calls == 1
    assert all(isinstance(r, AppError) and r.code == 'TRAINING_SUBMISSION_PENDING' for r in rest)
    finish.set()
    result = await first
    replay = await _submit(session, 2, request)
    assert result == replay and replay['tries']['left'] == 4
    assert calls == 1


async def test_expired_reservation_can_retry_and_fences_the_old_worker(store, session, monkeypatch):
    import asyncio
    await _submit(session, 1, _pick('b'))
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def grade(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await finish.wait()
            return llm.LLMResult(text='{"pass": true}', input_tokens=1, output_tokens=1)
        return llm.LLMResult(text='{"pass": false, "hint": "다른 이유는요?"}', input_tokens=1, output_tokens=1)

    monkeypatch.setattr(llm, 'generate', grade)
    request = _write('내가 못해서')
    first = asyncio.create_task(_submit(FakeSession(store), 2, request))
    await entered.wait()
    receipt = store.submissions[UID, TID, request.client_request_id]
    receipt.lease_until = trainings._now() - timedelta(seconds=1)
    replacement = await _submit(FakeSession(store), 2, request)
    finish.set()
    old = await first
    assert old == replacement
    assert old['result'] == 'retry' and old['tries']['left'] == 4
    assert (UID, TID, 2) not in store.answers
    assert calls == 2


async def test_completed_receipt_replays_after_progress_moves_on(store, session):
    request = _pick('b')
    await _submit(session, 1, request)
    await _submit(session, 2, _write('포장 실수'))
    replay = await _submit(session, 1, request)
    assert replay['answer']['stage'] == 1
    assert replay['progress']['next_stage'] == 3
    assert replay['tries']['left'] == 5


@pytest.mark.parametrize("failure", ["timeout", "provider", "malformed"])
async def test_fallback_reward_and_history_are_exactly_once(store, session, monkeypatch, failure):
    # Preserve a previously spent try as well as its refill schedule.
    await _submit(session, 1, _pick("a"))
    await _pass_up_to(session, 5)
    before_balance = store.balance
    before_rewards = len(store.rewards)
    calls = 0

    async def unavailable(*args, **kwargs):
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise TimeoutError("private provider details")
        if failure == "provider":
            raise RuntimeError("private provider details")
        return llm.LLMResult(text="invalid json", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(llm, "generate", unavailable)
    request = _write("오늘 끝까지 해낸 내가 고맙다")
    first = await _submit(session, 6, request)
    for _ in range(3):
        assert await _submit(session, 6, request) == first
    assert (first["result"], first["judge"], first["hint"]) == ("passed", "fallback", None)
    assert first["tries"]["left"] == 4
    assert store.tries[UID].refill_from == NOW
    assert first["progress"]["completed"]
    assert len(store.answers) == 6
    assert store.answers[UID, TID, 6].answer_text == request.text
    assert len(store.rewards) == before_rewards + 1
    assert store.balance == before_balance + 40
    assert calls == 1
    # A different request id cannot claim the completed stage again.
    with pytest.raises(AppError) as duplicate:
        await _submit(session, 6, _write(request.text))
    assert duplicate.value.code == "TRAINING_STAGE_NOT_CURRENT"
    assert store.balance == before_balance + 40


@pytest.mark.parametrize('tid', ['positive-circuit-v2', 'self-acceptance-v2'])
async def test_active_course_rewards_and_replayed_answers_are_not_duplicated(store, session, monkeypatch, tid):
    catalog = TrainingCatalog.load()
    monkeypatch.setattr(training_catalog, 'current', lambda: catalog)
    course = catalog.get(tid)
    for stage in course.stages:
        request = (_pick(next(o.id for o in stage.options if o.correct))
                   if stage.answer_type == 'pick' else _write(stage.grading.example.pick('ko')))
        result = await _submit(session, stage.number, request, training_id=tid)
        assert result['result'] == 'passed', stage.number
        before = store.balance
        replay = await _submit(session, stage.number, request, training_id=tid)
        assert replay == result
        assert store.balance == before
    assert [amount for _, _, amount in store.ledger] == [30, 60, 150]
    assert store.balance == 340
    assert len(store.answers) == 30
    assert len(store.rewards) == 3


@pytest.mark.parametrize('locale', ['ko', 'en', 'ja'])
async def test_acceptance_14_both_reasons_is_correct_after_retry(store, session, monkeypatch, locale):
    catalog = TrainingCatalog.load()
    monkeypatch.setattr(training_catalog, 'current', lambda: catalog)
    tid = 'self-acceptance-v2'
    store.progress[(UID, tid)] = TrainingProgress(user_id=UID, training_id=tid, next_stage=14)
    wrong = await _submit(session, 14, _pick('a'), locale=locale, training_id=tid)
    assert wrong['result'] == 'retry'
    right = await _submit(session, 14, _pick('c'), locale=locale, training_id=tid)
    assert right['result'] == 'passed'
    assert store.answers[(UID, tid, 14)].option_id == 'c'
    assert 'ai' not in store.events


# --- 구독 권한: 완료 replay는 보존하고 새 실행만 제한한다. ---


def _stored_state(store):
    tables = {}
    for name in ("submissions", "progress", "answers", "tries", "rewards"):
        tables[name] = {
            key: {column.key: getattr(row, column.key) for column in row.__table__.columns}
            for key, row in getattr(store, name).items()
        }
    return deepcopy((tables, store.balance, store.ledger))


@pytest.fixture(params=["pick", "write"])
def restricted_request(request, store, monkeypatch):
    course = CATALOG.get(TID)
    stage = course.stage(3 if request.param == "pick" else 6).model_copy(update={"number": 6})
    course = course.model_copy(update={"stages": (*course.stages[:5], stage)})
    catalog = TrainingCatalog((course,), {course.id: course})
    monkeypatch.setattr(training_catalog, "current", lambda: catalog)
    progress = trainings._new_progress(UID, TID, NOW)
    progress.next_stage = 6
    store.progress[UID, TID] = progress
    return _pick("c") if request.param == "pick" else _write("끝까지 해낸 내가 고맙다")


@pytest.mark.parametrize("environment", ["production", "development"])
async def test_free_restricted_submission_has_no_side_effects(
    store, session, monkeypatch, restricted_request, environment,
):
    monkeypatch.setattr(trainings.settings, "environment", environment)
    store.entitlement = {"plan": "free"}
    store.tries[UID] = TrainingTries(user_id=UID, tries_left=2, refill_from=NOW, updated_at=NOW)
    before = _stored_state(store)
    with pytest.raises(AppError) as blocked:
        await _submit(session, 6, restricted_request)
    assert (blocked.value.http_status, blocked.value.code, blocked.value.details) == (
        403, "TRAINING_SUBSCRIPTION_REQUIRED", {"free_stage_count": 5},
    )
    assert _stored_state(store) == before
    assert store.events == ["lock", "ensure"]
    assert store.entitlement_calls == [(USER, NOW)]


async def test_access_precedes_stage_and_tries_checks(store, session, restricted_request):
    store.entitlement = {"plan": "free"}
    store.progress.clear()
    store.tries[UID] = TrainingTries(user_id=UID, tries_left=0, refill_from=NOW, updated_at=NOW)
    before = _stored_state(store)
    with pytest.raises(AppError) as blocked:
        await _submit(session, 6, restricted_request)
    assert blocked.value.code == "TRAINING_SUBSCRIPTION_REQUIRED"
    assert _stored_state(store) == before
    assert "ai" not in store.events


@pytest.mark.parametrize("tid", ["positive-circuit-v2", "self-acceptance-v2", "refusal-practice-v1"])
@pytest.mark.parametrize("number", [6, 15, 30])
async def test_all_courses_apply_the_same_free_boundary(store, session, monkeypatch, tid, number):
    catalog = TrainingCatalog.load()
    monkeypatch.setattr(training_catalog, "current", lambda: catalog)
    stage = catalog.get(tid).stage(number)
    body = _pick(stage.options[0].id) if stage.answer_type == "pick" else _write("合成テストの回答です")
    store.entitlement = {"plan": "free"}
    store.progress[UID, tid] = TrainingProgress(user_id=UID, training_id=tid, next_stage=number)
    before = _stored_state(store)
    with pytest.raises(AppError) as blocked:
        await _submit(session, number, body, training_id=tid)
    assert blocked.value.code == "TRAINING_SUBSCRIPTION_REQUIRED"
    assert _stored_state(store) == before
    assert "ai" not in store.events


@pytest.mark.parametrize("tid", ["positive-circuit-v2", "self-acceptance-v2", "refusal-practice-v1"])
async def test_free_first_five_stages_keep_their_reward(store, session, monkeypatch, tid):
    catalog = TrainingCatalog.load()
    monkeypatch.setattr(training_catalog, "current", lambda: catalog)
    store.entitlement = {"plan": "free"}
    for stage in catalog.get(tid).stages[:5]:
        body = (_pick(next(option.id for option in stage.options if option.correct))
                if stage.answer_type == "pick" else _write("이 단계의 합성 테스트 답변"))
        result = await _submit(session, stage.number, body, training_id=tid)
        assert result["result"] == "passed"
    assert result["reward"] == {"granted": 30, "balance_after": 130}
    assert await _submit(session, 5, body, training_id=tid) == result
    assert len(store.ledger) == 1
    assert store.entitlement_calls == []


@pytest.mark.parametrize("plan,source,is_subscriber", [
    ("monthly", "subscription", True), ("yearly", "subscription", True),
    ("monthly", "store_trial", True), ("trial", "launch", False),
    ("trial", "signup_trial", False),
])
async def test_non_free_access_ignores_subscriber_flag_and_chat_quota(
    store, session, restricted_request, plan, source, is_subscriber,
):
    store.entitlement = {
        "plan": plan, "entitlement_source": source, "is_subscriber": is_subscriber,
        "tokens_remaining": 0,
    }
    result = await _submit(session, 6, restricted_request)
    assert result["result"] == "passed"
    assert store.entitlement_calls == [(USER, NOW)]


@pytest.mark.parametrize("passed", [True, False])
@pytest.mark.parametrize("legacy", [False, True])
async def test_completed_paid_request_replays_after_expiry(
    store, session, restricted_request, passed, legacy,
):
    if not passed:
        restricted_request = (_pick("a", restricted_request.client_request_id)
                              if restricted_request.option_id else restricted_request)
        store.ai_replies.append('{"pass": false, "hint": "다른 이유를 생각해 볼까요?"}')
    first = await _submit(session, 6, restricted_request)
    assert first["result"] == ("passed" if passed else "retry")
    if legacy:
        store.submissions.clear()
    store.entitlement = {"plan": "free"}
    store.entitlement_calls.clear()
    store.events.clear()
    before = _stored_state(store)
    replay = await _submit(session, 6, restricted_request)
    assert replay == first
    assert _stored_state(store) == before
    assert store.entitlement_calls == []
    assert store.events == ["lock", "ensure", "commit"]


async def test_changed_completed_request_is_not_hidden_by_subscription_error(
    store, session, restricted_request,
):
    await _submit(session, 6, restricted_request)
    store.entitlement = {"plan": "free"}
    store.entitlement_calls.clear()
    changed = (_pick("a", restricted_request.client_request_id) if restricted_request.option_id
               else _write("내용을 바꾼 합성 답변", restricted_request.client_request_id))
    before = _stored_state(store)
    for stage, body in [(6, changed), (1, _pick("b", restricted_request.client_request_id))]:
        with pytest.raises(AppError) as blocked:
            await _submit(session, stage, body)
        assert blocked.value.http_status == 422
    assert _stored_state(store) == before
    assert store.entitlement_calls == []


@pytest.mark.parametrize("lease_expired", [False, True])
async def test_unfinished_request_needs_current_access(store, session, lease_expired):
    await _pass_up_to(session, 5)
    body = _write("끝까지 해낸 내가 고맙다")
    receipt = await trainings._reserve(session, UID, CATALOG.get(TID), CATALOG.get(TID).stage(6), body)
    if lease_expired:
        receipt.lease_until = NOW - timedelta(seconds=1)
    store.entitlement = {"plan": "free"}
    store.events.clear()
    before = _stored_state(store)
    with pytest.raises(AppError) as blocked:
        await _submit(session, 6, body)
    assert blocked.value.code == "TRAINING_SUBSCRIPTION_REQUIRED"
    assert _stored_state(store) == before
    assert "ai" not in store.events


async def test_paid_unfinished_request_keeps_pending_without_another_grade(store, session):
    await _pass_up_to(session, 5)
    body = _write("끝까지 해낸 내가 고맙다")
    await trainings._reserve(session, UID, CATALOG.get(TID), CATALOG.get(TID).stage(6), body)
    store.events.clear()
    before = _stored_state(store)
    with pytest.raises(AppError) as blocked:
        await _submit(session, 6, body)
    assert (blocked.value.code, blocked.value.http_status) == ("TRAINING_SUBMISSION_PENDING", 503)
    assert _stored_state(store) == before
    assert store.entitlement_calls == [(USER, NOW)]
    assert "ai" not in store.events


async def test_other_user_cannot_replay_a_paid_request(store, session, restricted_request):
    await _submit(session, 6, restricted_request)
    store.entitlement = {"plan": "free"}
    store.entitlement_calls.clear()
    before = _stored_state(store)
    with pytest.raises(AppError) as blocked:
        await trainings.submit(session, str(OTHER), TID, 6, restricted_request, locale="ko", now=NOW)
    assert blocked.value.code == "TRAINING_SUBSCRIPTION_REQUIRED"
    assert _stored_state(store) == before
    assert store.entitlement_calls == [(str(OTHER), NOW)]


@pytest.mark.parametrize("offset_us", [-1, 0, 1])
async def test_access_uses_clock_after_lock_not_submit_time(
    store, session, monkeypatch, restricted_request, offset_us,
):
    clock = NOW - timedelta(seconds=1)
    profile = SimpleNamespace(trial_ends_at=NOW, language="ko")

    async def lock(session, uid):
        nonlocal clock
        store.events.append("lock")
        clock = NOW + timedelta(microseconds=offset_us)

    async def resolve(session, user_id, now):
        store.entitlement_calls.append((user_id, now))
        return entitlement.derive_entitlement(profile, None, 0, {}, now)

    monkeypatch.setattr(trainings, "advisory_xact_lock", lock)
    monkeypatch.setattr(trainings, "_now", lambda: clock)
    monkeypatch.setattr(gating, "resolve_entitlement", resolve)
    if offset_us < 0:
        assert (await _submit(session, 6, restricted_request, now=NOW - timedelta(days=1)))["result"] == "passed"
    else:
        before = _stored_state(store)
        with pytest.raises(AppError) as blocked:
            await _submit(session, 6, restricted_request, now=NOW - timedelta(days=1))
        assert blocked.value.code == "TRAINING_SUBSCRIPTION_REQUIRED"
        assert _stored_state(store) == before
        assert "ai" not in store.events
    assert store.entitlement_calls == [(USER, NOW + timedelta(microseconds=offset_us))]


async def test_admitted_grading_can_finish_after_expiry_with_current_balance(store, session):
    await _pass_up_to(session, 5)
    before_balance = store.balance

    def expiry_and_reward():
        store.entitlement = {"plan": "free"}
        store.balance += 50

    store.during_ai = expiry_and_reward
    result = await _submit(session, 6, _write("끝까지 해낸 내가 고맙다"))
    assert result["reward"] == {"granted": 40, "balance_after": before_balance + 50 + 40}
    assert store.entitlement_calls == [(USER, NOW)]


async def test_deletion_during_admitted_grading_still_blocks_recording(store, session, monkeypatch):
    await _pass_up_to(session, 5)
    before = _stored_state(store)

    async def deleted(session, uid):
        raise AppError("ACCOUNT_DELETING", 409, "계정 삭제를 처리하고 있어요.")

    def deleting():
        store.entitlement = {"plan": "free"}
        monkeypatch.setattr(privacy, "ensure_subject_active", deleted)

    store.during_ai = deleting
    with pytest.raises(AppError) as blocked:
        await _submit(session, 6, _write("끝까지 해낸 내가 고맙다"))
    assert blocked.value.code == "ACCOUNT_DELETING"
    assert len(store.submissions) == len(before[0]["submissions"]) + 1  # T1의 미완료 선점만 남는다.
    after = _stored_state(store)
    after[0]["submissions"] = before[0]["submissions"]
    assert after == before


@pytest.mark.parametrize("error", [
    AppError("NOT_FOUND", 404, "프로필을 찾을 수 없어요."), RuntimeError("lookup unavailable"),
])
async def test_access_lookup_failure_does_not_grade_or_mutate(
    store, session, monkeypatch, restricted_request, error,
):
    async def failed(*args, **kwargs):
        raise error

    monkeypatch.setattr(gating, "resolve_entitlement", failed)
    before = _stored_state(store)
    with pytest.raises(type(error)) as raised:
        await _submit(session, 6, restricted_request)
    assert raised.value is error
    assert _stored_state(store) == before
    assert store.events == ["lock", "ensure"]


async def test_free_read_and_claim_paths_do_not_resolve_access(store, session):
    store.entitlement = {"plan": "free"}
    await trainings.list_trainings(session, USER, locale="ko")
    await trainings.get_training(session, USER, TID, locale="ko", now=NOW)
    await trainings.claim(session, USER, TID, 6, TrainingClaimRequest(
        reason="should_pass", answer_text="합성 답변", verdict="retry",
    ))
    assert len(store.claims) == 1
    assert store.entitlement_calls == []
