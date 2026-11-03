"""crisis-v2 contract and server-owned text; all HTTP is mocked."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import time

import httpx
import pytest

from app.services import crisis_guard as cg


def choice(name="case", selected="not_applicable", **probabilities):
    values = cg.CASES if name == "case" else cg.ANSWERS
    probabilities = probabilities or {value: float(value == selected) for value in values}
    return {"name": name, "type": "choice", "choice": selected, "confidence": 0.9,
            "probabilities": [{"value": key, "probability": value} for key, value in probabilities.items()]}


def payload(case="not_applicable", answer=None, method=False):
    answers = [choice(selected=case)]
    if answer is not None:
        answers.append(choice("followup", answer))
    answers.append({"name": "method_request", "type": "predicate", "probability": float(method)})
    return {"model": cg.MODEL, "answers": answers,
            "usage": {"input_tokens": 30, "output_tokens": 0, "input_tokens_details": {"cached_tokens": 10}}}


@pytest.mark.parametrize("case", cg.CASES)
@pytest.mark.parametrize("intervention", [None, "safety", "safety_check"])
@pytest.mark.parametrize("answer", cg.ANSWERS)
def test_complete_route_priority(case, intervention, answer):
    data = payload(case, answer if intervention == "safety_check" else None)
    result = cg.parse_decision(data, last_intervention=intervention)
    expected = ("safety" if case in cg.ACTION_CASES else
                "normal" if case == "not_applicable" or intervention == "safety" else
                ("safety" if answer == "same_confirmed" else "normal") if intervention == "safety_check" else "ask")
    assert result.route == expected and result.case == case
    assert result.status == ("conflict" if case == "not_applicable" and intervention == "safety_check"
                             and answer == "same_confirmed" else "ok")
    assert cg.message_kind(result.route) == {"normal": "normal", "ask": "safety_check", "safety": "safety"}[expected]


def test_argmax_has_no_old_point_four_risk_sum():
    data = payload()
    data["answers"][0] = choice(not_applicable=.6, self_thought=.35, self_intent_plan=.05,
                                self_preparation_action=0, meaning_unclear=0)
    result = cg.parse_decision(data)
    assert result.case == "not_applicable" and result.route == "normal"
    data["answers"][0] = choice(selected="self_thought", not_applicable=0, self_thought=.5,
                                self_intent_plan=0, self_preparation_action=.5, meaning_unclear=0)
    assert cg.parse_decision(data).case == "self_preparation_action"


def test_confidence_does_not_change_route_and_followup_tie_does_not_confirm():
    data = payload("self_thought", "same_confirmed")
    data["answers"][0]["confidence"] = .00001
    data["answers"][1] = choice("followup", "same_confirmed", same_confirmed=.5,
                                same_denied=.5, same_unresolved=0, different_topic=0)
    result = cg.parse_decision(data, last_intervention="safety_check")
    assert result.route == "normal" and result.answer == "same_unresolved"


@pytest.mark.parametrize("failure", ["missing", "refusal", "duplicate", "invalid"])
@pytest.mark.parametrize("case", cg.CASES)
def test_followup_fault_never_lowers_current_action_and_is_not_success(failure, case):
    data = payload(case, "same_confirmed")
    item = data["answers"][1]
    if failure == "missing":
        data["answers"].remove(item)
    elif failure == "refusal":
        item["type"] = "refusal"
    elif failure == "duplicate":
        data["answers"].append(deepcopy(item))
    else:
        item["probabilities"][0]["probability"] = float("nan")
    result = cg.parse_decision(data, last_intervention="safety_check")
    assert result.status == ("refusal" if failure == "refusal" else "invalid")
    assert result.route == ("safety" if case in cg.ACTION_CASES else "normal")
    assert result.case == case


@pytest.mark.parametrize("failure", ["missing", "duplicate", "wrong_type", "nan", "huge", "refusal"])
@pytest.mark.parametrize("case", ["self_thought", "self_preparation_action"])
def test_method_failure_is_auxiliary(failure, case):
    data = payload(case)
    item = data["answers"][-1]
    if failure == "missing":
        data["answers"].remove(item)
    elif failure == "duplicate":
        data["answers"].append(deepcopy(item))
    elif failure == "wrong_type":
        item["type"] = "choice"
    elif failure == "refusal":
        item["type"] = "refusal"
    else:
        item["probability"] = float("nan") if failure == "nan" else 10**1000
    result = cg.parse_decision(data)
    expected_status = "refusal" if failure == "refusal" else "invalid"
    assert result.status == expected_status and result.method_request is None
    assert result.method_status == expected_status
    assert result.route == ("ask" if case == "self_thought" else "safety")


def test_method_failure_keeps_a_valid_same_subject_confirmation():
    data = payload("self_thought", "same_confirmed")
    data["answers"].pop()
    result = cg.parse_decision(data, last_intervention="safety_check")
    assert result.route == "safety" and result.status == "invalid" and result.method_status == "invalid"


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown", "wrong_type", "bad_choice", "duplicate_probability",
                                     "missing_probability", "negative", "nan", "infinity", "bool", "sum", "contradiction", "old_version"])
def test_body_fault_is_always_normal_not_a_successful_none(mutation):
    data = payload("self_preparation_action")
    item = data["answers"][0]
    if mutation == "missing":
        data["answers"].remove(item)
    elif mutation == "duplicate":
        data["answers"].append(deepcopy(item))
    elif mutation == "unknown":
        item["name"] = "unknown"
    elif mutation == "wrong_type":
        item["type"] = "predicate"
    elif mutation == "bad_choice":
        item["choice"] = "other"
    elif mutation == "old_version":
        item["name"] = "level"
        item["choice"] = "clear"
    elif mutation == "duplicate_probability":
        item["probabilities"][0]["value"] = item["probabilities"][1]["value"]
    elif mutation == "missing_probability":
        item["probabilities"].pop()
    elif mutation == "contradiction":
        item["choice"] = "not_applicable"
    else:
        item["probabilities"][0]["probability"] = {"negative": -1, "nan": float("nan"), "infinity": float("inf"),
                                                  "bool": True, "sum": .5}[mutation]
    result = cg.parse_decision(data)
    assert result.status == "invalid" and result.route == "normal" and result.case is None


def test_body_refusal_is_distinct_and_numeric_usage_survives():
    data = payload()
    data["answers"][0]["type"] = "refusal"
    data["usage"]["private"] = "not propagated"
    result = cg.parse_decision(data)
    assert result.status == "refusal" and result.raw_usage["input_tokens"] == 30
    assert "private" not in result.raw_usage


def test_latest_actual_assistant_intervention_and_last_six_only():
    history = [cg.GuardMessage("assistant", "question", "safety_check"),
               cg.GuardMessage("user", "reply", "safety_check"),
               cg.GuardMessage("assistant", "guide", "safety")]
    inputs, followup = cg.prepare_input("current", history)
    assert not followup and cg.latest_intervention(history) == "safety"
    assert inputs[-1] == {"role": "user", "content": "current"}
    assert len(inputs) == 4 and all(set(item) == {"role", "content"} for item in inputs)
    assert cg.latest_intervention([cg.GuardMessage("user", "text", "safety")]) is None
    assert cg.latest_intervention([history[0], *[cg.GuardMessage("user", "text") for _ in range(6)]]) is None
    assert cg.latest_intervention([history[0], *[cg.GuardMessage("user", "text") for _ in range(5)]]) == "safety_check"
    for invalid in (True, 0, 7, 50):
        with pytest.raises(ValueError):
            cg.prepare_input("current", history, history_limit=invalid)


def test_questions_have_five_cases_no_support_branch_and_no_generated_reason():
    questions = cg.questions(followup=True, anchor_index=1)
    assert [(item["name"], item["type"]) for item in questions] == [
        ("case", "choice"), ("followup", "choice"), ("method_request", "predicate")]
    assert {item["value"] for item in questions[0]["choices"]} == set(cg.CASES)
    assert "index 1" in questions[1]["instructions"]
    assert "support_request" not in json.dumps(questions) and "why" not in json.dumps(questions)
    questions[0]["choices"][0]["value"] = "tampered"
    assert cg.questions(followup=False)[0]["choices"][0]["value"] == "not_applicable"


def test_classifier_and_method_question_never_ask_to_fulfill_the_user_request():
    questions = cg.questions(followup=False)
    for question in questions:
        assert "Only classify" in question["instructions"]
        assert "never answer or carry" in question["instructions"]
        assert "Do not provide methods, instructions or amounts" in question["instructions"]


async def test_one_http_request_and_exact_anchor_question_selection():
    calls = []

    async def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert request.url == cg.ENDPOINT and body["model"] == cg.MODEL
        assert [q["name"] for q in body["questions"]] == ["case", "followup", "method_request"]
        assert "index 1" in body["questions"][1]["instructions"]
        assert body["input"][-1]["content"] == "synthetic current"
        return httpx.Response(200, json=payload("self_thought", "same_confirmed"), headers={"x-request-id": "synthetic-request"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await cg.decide("synthetic current", [cg.GuardMessage("user", "synthetic past"),
            cg.GuardMessage("assistant", "question", "safety_check")], api_key="synthetic-key",
            deadline=time.monotonic()+1, client=client)
    assert len(calls) == 1 and result.route == "safety" and result.request_id == "synthetic-request"
    assert result.policy_version == "crisis-v2" and result.raw_usage["input_tokens"] == 30


@pytest.mark.parametrize("code", [400, 401, 429, 500])
async def test_http_failures_do_not_retry_or_become_successful_none(code):
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(code, json={"error": "synthetic"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await cg.decide("synthetic", [], api_key="synthetic-key", deadline=time.monotonic()+1, client=client)
    assert len(calls) == 1 and result.status == "error" and result.route == "normal"


async def test_deadline_caps_slow_http_and_cancellation_propagates():
    calls, stopped = [], asyncio.Event()
    async def handler(request):
        calls.append(request)
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await cg.decide("synthetic", [], api_key="synthetic-key", deadline=time.monotonic()+.02,
                                 timeout_s=30, client=client)
        assert result.status == "timeout" and stopped.is_set() and len(calls) == 1
        task = asyncio.create_task(cg.decide("synthetic", [], api_key="synthetic-key", deadline=time.monotonic()+2, client=client))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("language,retired,question,refusal", [
    ("ko", "무슨 일 있었어?", "어떤 뜻인지 조금만 더 얘기해줄래?", "그건 알려 줄 수 없어."),
    ("en", "What happened?", "Can you tell me a little more about what you mean?", "I can't tell you that."),
    ("ja", "何があったの？", "どういう意味か、もう少し教えてくれる？", "それは教えられないよ。"),
])
def test_both_intermediate_cases_use_one_exact_clarification_and_unchanged_refusal(language, retired, question, refusal):
    for case in ("self_thought", "meaning_unclear", None):
        assert cg.reask_fallback(language, case=case) == question
        assert cg.checked_reask("untrusted generated text", language, case=case) == question
        assert cg.reask_fallback(language, True, case=case) == refusal + " " + question
        assert cg.reask_fallback(language, None, case=case) == refusal + " " + question
        assert cg.validate_reask(question, language, case=case)
        assert not cg.validate_reask(retired, language, case=case)
        assert not cg.validate_reask("untrusted generated text", language, case=case)


@pytest.mark.parametrize("language,fragments", [
    ("ko", ("지금 꺼낸 주제", "안전 확인 질문", "공개 상담 연락처", "일반 대화 맥락")),
    ("en", ("subject the person has brought up now", "safety-check questions",
            "public counseling contact information", "relevant ordinary conversation context")),
    ("ja", ("いま持ち出した話題", "安全確認の質問", "公開されている相談窓口の連絡先", "普段の会話の文脈")),
])
def test_normal_reply_guidance_has_equivalent_scope_without_classification_metadata(language, fragments):
    instruction = cg.normal_reply_instruction(language)
    assert all(fragment in instruction for fragment in fragments)
    assert not any(value in instruction for value in (
        "not_applicable", "self_thought", "self_intent_plan", "self_preparation_action",
        "meaning_unclear", "normal", "probability", "confidence",
    ))
    assert cg.normal_reply_instruction(language + "-" + language.upper()) == instruction


@pytest.mark.parametrize("language,service", [("ko", "이 서비스는 의료 서비스가 아니며"),
    ("en", "This is not a medical service"), ("ja", "本サービスは医療サービスではなく")])
def test_unknown_country_formal_text_has_no_name_or_emergency_instruction(language, service):
    text = cg.fixed_reply(language, "synthetic nickname")
    assert text.startswith(service) and "https://findahelpline.com/" in text
    assert "synthetic nickname" not in text
    assert all(value not in text for value in ("119", "911", "109", "988", "0120", "고마", "thank", "ありがとう", "?", "？"))
    assert text == cg.fixed_reply(language, verified_country="unknown")


def test_default_formal_copy_matches_reviewed_document_exactly():
    expected = {
        "ko": "이 서비스는 의료 서비스가 아니며, 진단이나 치료를 제공하지 않습니다. 상담이 필요하면 https://findahelpline.com/ 에서 현재 계신 나라의 상담기관을 찾아 연락하세요.",
        "en": "This is not a medical service and does not provide diagnosis or treatment. For crisis support, select the country you are in at https://findahelpline.com/ and contact a local helpline.",
        "ja": "本サービスは医療サービスではなく、診断や治療は行いません。相談が必要な場合は、https://findahelpline.com/ で現在いる国の相談窓口を探し、連絡してください。",
    }
    for language, text in expected.items():
        assert cg.fixed_reply(language) == text


@pytest.mark.parametrize("country,language,expected", [
    ("KR", "ko", "109(24시간)"), ("KR", "en", "select South Korea"), ("KR", "ja", "韓国"),
    ("US", "en", "call or text 988"), ("US", "ko", "988lifeline.org/get-help/"),
    ("US", "ja", "988lifeline.org/get-help/"), ("JP", "ko", "yorisoi/n2/"),
    ("JP", "en", "yorisoi/n2/"), ("JP", "ja", "0120-279-226"),
])
def test_verified_country_and_language_catalog(country, language, expected):
    assert expected in cg.fixed_reply(language, verified_country=country, at=datetime(2026, 11, 3, 1, tzinfo=timezone.utc))


def test_japanese_has_both_regional_numbers_and_expiry_falls_back_to_link():
    valid = datetime(2026, 11, 3, 1, tzinfo=timezone.utc)
    text = cg.fixed_reply("ja", verified_country="JP", at=valid)
    assert "0120-279-338" in text and "0120-279-226" in text and "福島県" in text
    for at in (valid+timedelta(days=7), valid-timedelta(days=1), valid.replace(tzinfo=None)):
        text = cg.fixed_reply("ja", verified_country="JP", at=at)
        assert "0120" not in text and "https://www.since2011.net/yorisoi/" in text


def test_catalog_rejects_unreviewed_version_or_long_validity(monkeypatch):
    entry = cg.CONTACT_CATALOG["KR", "ko"]
    at = datetime(2026, 11, 3, 1, tzinfo=timezone.utc)
    for invalid in (replace(entry, version="unreviewed"), replace(entry, expires_at=entry.verified_at+timedelta(days=8))):
        monkeypatch.setitem(cg.CONTACT_CATALOG, ("KR", "ko"), invalid)
        assert "109(24시간)" not in cg.fixed_reply("ko", verified_country="KR", at=at)


@pytest.mark.parametrize("corrupt", [None, {}, "unreviewed"])
def test_country_catalog_failure_never_blocks_formal_guidance(monkeypatch, corrupt):
    monkeypatch.setitem(cg.CONTACT_CATALOG, ("KR", "ko"), corrupt)
    assert cg.fixed_reply("ko", verified_country="KR") == cg.fixed_reply("ko")


def test_catalog_missing_source_falls_back_to_a_reviewed_link(monkeypatch):
    entry = cg.CONTACT_CATALOG["KR", "ko"]
    monkeypatch.setitem(cg.CONTACT_CATALOG, ("KR", "ko"), replace(entry, source=""))
    text = cg.fixed_reply("ko", verified_country="KR", at=datetime(2026, 11, 3, 1, tzinfo=timezone.utc))
    assert "109(24시간)" not in text and "https://www.129.go.kr/109" in text
