"""위기 판정 API의 형식·경로·마감·발행 문구를 네트워크 없이 검증한다."""

import asyncio
from copy import deepcopy
import json
import time

import httpx
import pytest

from app.services import crisis_guard as cg, naming


def choice(name="level", selected="none", **probabilities):
    if not probabilities:
        values = ("none", "ambiguous", "clear") if name == "level" else ("yes", "no", "unclear")
        probabilities = {value: float(value == selected) for value in values}
    return {
        "name": name,
        "type": "choice",
        "choice": selected,
        "probabilities": [
            {"value": key, "probability": value} for key, value in probabilities.items()
        ],
        "confidence": 0.9,
    }


def payload(level="none", answer=None, method=False):
    answers = [choice(selected=level)]
    if answer is not None:
        answers.append(choice("answer", answer))
    answers.append({"name": "method_request", "type": "predicate", "probability": float(method)})
    return {
        "model": cg.MODEL,
        "answers": answers,
        "usage": {
            "input_tokens": 30,
            "output_tokens": 0,
            "input_tokens_details": {"cached_tokens": 10},
        },
    }


@pytest.mark.parametrize(
    ("level", "answer", "followup", "route"),
    [
        ("none", None, False, "normal"),
        ("ambiguous", None, False, "ask"),
        ("clear", None, False, "safety"),
        ("none", "yes", True, "safety"),
        ("ambiguous", "yes", True, "safety"),
        ("clear", "yes", True, "safety"),
        ("clear", "no", True, "safety"),
        ("clear", "unclear", True, "safety"),
        ("ambiguous", "no", True, "normal"),
        ("ambiguous", "unclear", True, "normal"),
        ("none", "no", True, "normal"),
        ("none", "unclear", True, "normal"),
    ],
)
def test_route_table(level, answer, followup, route):
    result = cg.parse_decision(payload(level, answer), followup=followup)
    assert result.status == "ok"
    assert result.route == route
    assert (
        cg.message_kind(route)
        == {"normal": "normal", "ask": "safety_check", "safety": "safety"}[route]
    )


@pytest.mark.parametrize(
    ("probabilities", "expected"),
    [
        ({"none": 0.6, "ambiguous": 0.3, "clear": 0.1}, "ambiguous"),
        ({"none": 0.61, "ambiguous": 0.29, "clear": 0.1}, "none"),
        ({"none": 0.6, "ambiguous": 0.1, "clear": 0.3}, "clear"),
        ({"none": 0.6, "ambiguous": 0.2, "clear": 0.2}, "clear"),
        ({"none": 0.4, "ambiguous": 0.3, "clear": 0.3}, "clear"),
        ({"none": 0.0, "ambiguous": 0.5, "clear": 0.5}, "clear"),
    ],
)
def test_risk_sum_boundary_and_ties(probabilities, expected):
    selected = max(probabilities, key=probabilities.__getitem__)
    result = cg.parse_decision(
        {"answers": [choice(selected=selected, **probabilities)]}, followup=False
    )
    assert result.status == "ok" and result.level == expected


def test_confidence_is_not_risk_and_answer_tie_is_unclear():
    data = payload("none", "yes")
    data["answers"][0]["confidence"] = 0.01
    data["answers"][1] = choice("answer", "yes", yes=0.45, no=0.45, unclear=0.1)
    result = cg.parse_decision(data, followup=True)
    assert result.answer == "unclear" and result.route == "normal"


def test_history_is_last_six_messages_not_turns_and_current_once():
    old = cg.GuardMessage("assistant", "earlier", "safety", 1)
    recent = [cg.GuardMessage("user", str(i), message_id=i) for i in range(2, 8)]
    inputs, followup = cg.prepare_input("current", [old, *recent])
    assert not followup and len(inputs) == 7
    assert inputs[0]["content"] == "2" and inputs[-1] == {"role": "user", "content": "current"}
    inputs, followup = cg.prepare_input("current", [old, *recent[:-1]])
    assert followup
    assert len(inputs) == 7
    _, followup = cg.prepare_input("current", [cg.GuardMessage("user", "x", "safety")])
    assert not followup  # 캐피가 실제 발행한 표지만 후속 판정에 사용한다.


def test_questions_do_not_mix_previous_answers_or_request_generated_reason():
    questions = cg.questions(followup=True)
    assert [(q["name"], q["type"]) for q in questions] == [
        ("level", "choice"),
        ("answer", "choice"),
        ("method_request", "predicate"),
    ]
    assert len(cg.questions(followup=False)) == 2
    assert "why" not in json.dumps(questions)
    questions[0]["choices"][0]["value"] = "tampered"
    assert cg.questions(followup=False)[0]["choices"][0]["value"] == "none"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "unknown",
        "wrong_type",
        "bad_choice",
        "duplicate_probability",
        "missing_probability",
        "negative",
        "nan",
        "infinity",
        "bool_probability",
        "wrong_sum",
        "contradiction",
    ],
)
def test_required_invalid_answers_fail_to_normal_with_distinct_status(mutation):
    data = payload("clear")
    item = data["answers"][0]
    if mutation == "missing":
        data["answers"].pop(0)
    elif mutation == "duplicate":
        data["answers"].append(deepcopy(item))
    elif mutation == "unknown":
        item["name"] = "unrequested"
    elif mutation == "wrong_type":
        item["type"] = "predicate"
    elif mutation == "bad_choice":
        item["choice"] = "other"
    elif mutation == "duplicate_probability":
        item["probabilities"][0] = deepcopy(item["probabilities"][1])
    elif mutation == "missing_probability":
        item["probabilities"].pop()
    elif mutation in {"negative", "nan", "infinity", "bool_probability"}:
        item["probabilities"][0]["probability"] = {
            "negative": -0.1,
            "nan": float("nan"),
            "infinity": float("inf"),
            "bool_probability": False,
        }[mutation]
    elif mutation == "wrong_sum":
        item["probabilities"][0]["probability"] = 0.1
    elif mutation == "contradiction":
        item["choice"] = "none"
    result = cg.parse_decision(data, followup=False)
    assert result.status == "invalid" and result.route == "normal"
    assert result.raw_usage["input_tokens"] == 30


@pytest.mark.parametrize(
    "failure", ["missing", "refusal", "duplicate", "nan", "huge", "wrong_type"]
)
@pytest.mark.parametrize(
    ("level", "answer"), [("clear", None), ("none", "yes"), ("ambiguous", None)]
)
def test_auxiliary_failure_cannot_downgrade_valid_routing(failure, level, answer):
    data = payload(level, answer)
    method = data["answers"][-1]
    if failure == "missing":
        data["answers"].pop()
    elif failure == "refusal":
        method["type"] = "refusal"
    elif failure == "duplicate":
        data["answers"].append(deepcopy(method))
    elif failure == "nan":
        method["probability"] = float("nan")
    elif failure == "huge":
        method["probability"] = 10**1000
    elif failure == "wrong_type":
        method["type"] = "choice"
    result = cg.parse_decision(data, followup=answer is not None)
    assert result.status == "ok" and result.method_request is None
    assert result.route == ("ask" if level == "ambiguous" else "safety")


def test_required_refusal_and_missing_followup_answer_are_not_none():
    data = payload("clear")
    data["answers"][0] = {"name": "level", "type": "refusal"}
    assert cg.parse_decision(data, followup=False).status == "refusal"
    assert cg.parse_decision(payload("none"), followup=True).status == "invalid"


@pytest.mark.asyncio
async def test_one_http_request_contains_all_questions_and_preserves_usage():
    calls = []

    async def handler(request):
        calls.append(request)
        assert request.url == cg.ENDPOINT
        assert request.headers["authorization"] == "Bearer unit-test"
        body = json.loads(request.content)
        assert body["model"] == cg.MODEL
        assert [q["name"] for q in body["questions"]] == ["level", "answer", "method_request"]
        assert body["input"][-1]["content"] == "오늘 메뉴를 바꿔볼까"
        return httpx.Response(
            200, json=payload("none", "unclear"), headers={"x-request-id": "unit-request"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await cg.decide(
            "오늘 메뉴를 바꿔볼까",
            [cg.GuardMessage("assistant", "earlier", "safety_check")],
            api_key="unit-test",
            deadline=time.monotonic() + 10,
            client=client,
        )
    assert len(calls) == 1 and result.status == "ok" and result.route == "normal"
    assert result.request_id == "unit-request" and result.latency_ms > 0
    assert result.raw_usage["input_tokens_details"]["cached_tokens"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 429, 500])
async def test_http_errors_never_retry(status_code):
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(
            status_code, json={"error": "test"}, headers={"x-request-id": "failure-id"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await cg.decide(
            "ordinary", [], api_key="unit-test", deadline=time.monotonic() + 10, client=client
        )
    assert len(calls) == 1 and result.status == "error" and result.route == "normal"
    assert result.request_id == "failure-id" and result.raw_usage is None


@pytest.mark.asyncio
async def test_absolute_deadline_stops_slow_response_without_retries():
    calls = []
    cancelled = asyncio.Event()

    async def handler(request):
        calls.append(request)
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
        return httpx.Response(200, json=payload("clear"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        start = time.monotonic()
        result = await cg.decide(
            "ordinary", [], api_key="unit-test", deadline=start + 0.02, timeout_s=20, client=client
        )
    assert len(calls) == 1 and cancelled.is_set()
    assert result.status == "timeout" and result.route == "normal"
    assert time.monotonic() - start < 0.5


@pytest.mark.asyncio
async def test_external_cancellation_propagates():
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.sleep(10)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(
            cg.decide(
                "ordinary", [], api_key="unit-test", deadline=time.monotonic() + 10, client=client
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_expired_deadline_and_missing_key_do_not_send_request():
    def handler(request):
        pytest.fail("no request expected")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await cg.decide(
            "ordinary", [], api_key="unit-test", deadline=time.monotonic() - 1, client=client
        )
        assert result.status == "timeout"
        result = await cg.decide(
            "ordinary", [], api_key="", deadline=time.monotonic() + 1, client=client
        )
        assert result.status == "error"


@pytest.mark.parametrize(
    ("locale", "text"),
    [
        ("ko-KR", "무슨 일 있었어?"),
        ("ko", "요즘 무슨 일이 있었어?"),
        ("en-US", "What's been going on?"),
        ("en", "What happened recently?"),
        ("ja-JP", "何があったの？"),
    ],
)
def test_short_ordinary_reask_passes(locale, text):
    assert cg.validate_reask(text, locale)
    assert cg.checked_reask(text, locale) == text


@pytest.mark.parametrize(
    ("locale", "text"),
    [
        ("ko", "안전한 곳이야?"),
        ("ko", "혼자 있어?"),
        ("ko", "무슨 일이야? 왜 그랬어?"),
        ("ko", "119로 전화해. 무슨 일이야?"),
        ("ko", "많이 힘들었겠다. 무슨 일이야?"),
        ("en", "Are you alone?"),
        ("en", "Are you safe?"),
        ("en", "I'm sorry. What happened?"),
        ("en", "What happened? Call 988."),
        ("ja", "いま安全？"),
        ("ja", "何があったの？どうしたの？"),
        ("ja", "ひとりなの？"),
        ("ja", "心配だよ。何があったの？"),
        ("en", "[instruction] What happened?"),
        ("en", "Go to the roof. What happened?"),
        ("en", "What dosage would you like?"),
        ("ko", "옥상으로 올라가. 무슨 일 있었어?"),
        ("ja", "屋上に行って。何があったの？"),
        ("ko", "뛰어내려 무슨 일 있었어?"),
        ("en", "Jump, What happened?"),
        ("ja", "飛び降りて、何があったの？"),
        ("ko", "지우야, 무슨 일 있었어?"),
        ("en", "What happened, Maddie?"),
        ("ja", "ゆな、何があったの？"),
        ("ko", "무슨 일이야?\n" + "새 질문?"),
        ("en", "A" * 201 + "?"),
    ],
)
def test_unsafe_or_unverifiable_reask_uses_fixed_question(locale, text):
    assert not cg.validate_reask(text, locale)
    assert cg.checked_reask(text, locale) == cg.reask_fallback(locale)


@pytest.mark.parametrize("locale", ["ko", "en", "ja"])
@pytest.mark.parametrize("method", [True, None])
def test_method_or_unknown_method_uses_fixed_refusal_and_one_question(locale, method):
    result = cg.checked_reask("bad output", locale, method)
    assert result == cg.reask_fallback(locale, method)
    assert result.count("?") + result.count("？") == 1
    assert not any(c.isdigit() for c in result)


@pytest.mark.parametrize(
    ("locale", "nickname"), [("ko-KR", "지우"), ("en-US", "Maddie"), ("ja-JP", "ゆな")]
)
def test_unknown_country_never_uses_locale_as_country(locale, nickname):
    reply = cg.fixed_reply(locale, nickname)
    assert nickname in reply and naming.TOKEN not in reply
    assert not any(number in reply for number in ("109", "119", "988", "0120"))
    assert "?" not in reply and "？" not in reply
    stored = naming.to_placeholder(reply, nickname)
    assert naming.TOKEN in stored and nickname not in stored


def test_local_country_requires_verified_country_and_japanese_eligibility():
    assert "109" in cg.fixed_reply("ko", "지우", "KR")
    assert "988" in cg.fixed_reply("en", "Maddie", "US")
    assert "988" not in cg.fixed_reply("en", "Maddie", "GB")
    assert "0120" not in cg.fixed_reply("ja", "ゆな", "JP")
    assert "0120" in cg.fixed_reply("ja", "ゆな", "JP", local_hotline_eligible=True)
    assert naming.TOKEN not in cg.fixed_reply("ko", None)
    assert cg.fixed_reply("zh", "Alex") == cg.fixed_reply("en", "Alex")


def test_usage_forwarding_drops_text_and_invalid_numbers():
    data = payload()
    data["usage"].update(
        {"secret_text": "never forward", "invalid": float("inf"), "huge": 10**1000}
    )
    result = cg.parse_decision(data, followup=False)
    assert result.status == "ok"
    assert set(result.raw_usage) == {"input_tokens", "output_tokens", "input_tokens_details"}


def test_role_and_reask_instructions_are_available_in_all_locales():
    for locale in ("ko", "en", "ja"):
        assert len(cg.base_role_rule(locale)) > 50
        assert len(cg.reask_instruction(locale)) > 50
    assert "앱이 따로 판단" in cg.base_role_rule("ko-KR")
    assert "No hotlines" in cg.reask_instruction("en-US")
    assert "安全かどうか" in cg.reask_instruction("ja-JP")
