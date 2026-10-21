"""트레이닝 서비스 흐름 — 메모리 저장소로 DB 읽기·쓰기 함수를 바꿔 끼운다.

주관식 제출은 트랜잭션 1(읽기) → AI 채점 → 트랜잭션 2(잠금 아래 재확인·쓰기)다. AI 호출 중에
저장소를 바꿔 그사이 다른 제출이 끼어든 경우를 흉내 낸다.
"""

from __future__ import annotations

import uuid
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
)
from app.schemas.trainings import TrainingAnswerRequest, TrainingClaimRequest
from app.services import hay_ledger, llm, privacy, training_catalog, trainings
from app.services.training_catalog import TrainingCatalog

CATALOG = TrainingCatalog.load(Path(__file__).parent / "fixtures/trainings/catalog.json")
TID = "practice-basics"
UID = uuid.UUID("00000000-0000-4000-8000-000000000001")
OTHER = uuid.UUID("00000000-0000-4000-8000-000000000002")
USER = str(UID)
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)


class Store:
    def __init__(self) -> None:
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


class FakeSession:
    def __init__(self, store: Store) -> None:
        self.store = store

    def add(self, row) -> None:
        s = self.store
        if isinstance(row, TrainingProgress):
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
    monkeypatch.setattr(trainings, "_load_progress", load_progress)
    monkeypatch.setattr(trainings, "_load_all_progress", load_all_progress)
    monkeypatch.setattr(trainings, "_load_answers", load_answers)
    monkeypatch.setattr(trainings, "_load_answer", load_answer)
    monkeypatch.setattr(trainings, "_load_tries", load_tries)
    monkeypatch.setattr(trainings, "_reward_granted", reward_granted)
    monkeypatch.setattr(trainings, "advisory_xact_lock", lock)
    monkeypatch.setattr(privacy, "ensure_subject_active", ensure)
    monkeypatch.setattr(trainings, "_account_language", account_language)
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
    assert store.events == ["ensure", "commit", "ai", "lock", "ensure", "commit"]


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


async def test_claims_are_saved_for_write_stages_only(store, session):
    body = TrainingClaimRequest(reason="should_pass", answer_text=" 포장 실수 ", verdict="retry", hint="다시 써 볼까요?")
    await trainings.claim(session, USER, TID, 2, body)
    claim = store.claims[0]
    assert (claim.user_id, claim.stage, claim.reason, claim.answer_text, claim.verdict) == (
        UID, 2, "should_pass", "포장 실수", "retry",
    )
    with pytest.raises(AppError) as pick:
        await trainings.claim(session, USER, TID, 1, body)
    assert pick.value.http_status == 422
    with pytest.raises(AppError) as missing:
        await trainings.claim(session, USER, TID, 9, body)
    assert missing.value.code == "TRAINING_NOT_FOUND"
