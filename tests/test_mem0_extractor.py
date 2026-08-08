"""후보 추출 파서 — 근거 구절을 원문과 대조한다.

검증하지 않으면 모델이 지어낸 근거로 기억이 만들어지고, 나중에 "어디서 그 말을 들었냐"에
답할 수 없다.

v3에서 두 가지가 바뀌었다.
 · 근거를 바이트 좌표가 아니라 **원문에서 복사한 구절**로 받는다. 좌표는 서버가 계산한다
 · **전량 폐기는 출력을 통째로 못 믿을 때만** 한다. 후보 하나의 문제는 그 후보만 버린다
"""
from __future__ import annotations

import json

import pytest

from app.services import mem0_extractor as ex

MSG = ex.SourceMessage.sanitized(id=11, sender="user", content="나는 커피를 좋아해")
ASSISTANT = ex.SourceMessage.sanitized(id=12, sender="moly", content="좋아하는구나")


def _out(**kw):
    base = {"text": "커피를 좋아한다", "category": "preference",
            "evidence": [{"message_id": 11, "quote": "커피를 좋아해"}]}
    base.update(kw)
    return json.dumps({"candidates": [base]}, ensure_ascii=False)


# ── 정상 ─────────────────────────────────────────────────────
def test_valid_candidate_is_parsed_with_verified_hash():
    got, dropped = ex.parse(_out(), messages=[MSG])
    assert dropped == [] and len(got) == 1
    span = got[0].evidence[0]
    assert span.message_id == 11 and span.sender == "user"
    # 좌표도 hash도 모델이 준 게 아니라 **우리가 원문에서 계산**한다.
    assert MSG.utf8[span.start_utf8:span.end_utf8].decode() == "커피를 좋아해"
    assert span.content_hash == MSG.hash_of(span.start_utf8, span.end_utf8)


def test_empty_candidates_is_valid():
    got, dropped = ex.parse('{"candidates":[]}', messages=[MSG])
    assert got == [] and dropped == []


def test_code_fence_is_tolerated():
    fenced = "```json\n" + _out() + "\n```"
    got, _ = ex.parse(fenced, messages=[MSG])
    assert len(got) == 1


def test_quote_with_different_spacing_still_matches():
    """모델이 공백을 흘리거나 줄바꿈을 공백으로 바꾸는 일이 잦다 — 그 정도는 구제한다."""
    msg = ex.SourceMessage.sanitized(id=1, sender="user", content="나는 커피를  아주 좋아해")
    got, dropped = ex.parse(
        json.dumps({"candidates": [{"text": "커피를 좋아한다", "category": "preference",
                                    "evidence": [{"message_id": 1, "quote": "커피를 아주 좋아해"}]}]},
                   ensure_ascii=False),
        messages=[msg])
    assert dropped == [] and len(got) == 1
    assert msg.utf8[got[0].evidence[0].start_utf8:got[0].evidence[0].end_utf8].decode() \
        == "커피를  아주 좋아해"


# ── 전량 폐기는 **출력을 통째로 못 믿을 때만** ──────────────────
@pytest.mark.parametrize(
    "payload",
    ["not json", '["array top level"]', '{"candidates": "not a list"}'],
)
def test_only_unusable_output_discards_everything(payload):
    with pytest.raises(ex.ExtractionSchemaError):
        ex.parse(payload, messages=[MSG])


def test_truncated_output_is_its_own_error():
    """잘림은 재시도해도 같다 — 형식 오류와 구분해야 덩어리를 쪼갤 수 있다."""
    with pytest.raises(ex.OutputTruncated):
        ex.parse(_out(), messages=[MSG], finish_reason="length")


# ── 후보 하나의 문제는 그 후보만 버린다 ──────────────────────
#
# 예전에는 아래가 전부 전량 폐기였다. 후보 24개 중 하나만 어긋나도 0건이 되고, 같은 입력으로
# 8번 재시도해 잡이 죽고, 그 사람의 기억이 통째로 멈췄다.
def test_unknown_message_id_drops_only_that_candidate():
    got, dropped = ex.parse(
        _out(evidence=[{"message_id": 999, "quote": "x"}]), messages=[MSG])
    assert got == [] and dropped[0][1] == "unknown_message_id"


def test_bad_evidence_shape_drops_only_that_candidate():
    got, dropped = ex.parse(
        _out(evidence=[{"message_id": "11", "quote": 3}]), messages=[MSG])
    assert got == [] and dropped[0][1] == "bad_evidence_shape"


def test_empty_text_drops_only_that_candidate():
    got, dropped = ex.parse(
        '{"candidates": [{"text": "", "category": "preference", "evidence": []}]}',
        messages=[MSG])
    assert got == [] and dropped[0][1] == "empty_text"


def test_unknown_category_is_absorbed_not_discarded():
    """라벨만 틀린 경우까지 버리지 않는다 — 본문은 멀쩡하다."""
    got, dropped = ex.parse(_out(category="hobby"), messages=[MSG])
    assert len(got) == 1 and got[0].category == ex.FALLBACK_CATEGORY
    assert dropped == []


def test_one_bad_candidate_does_not_kill_the_good_ones():
    payload = json.dumps({"candidates": [
        {"text": "커피를 좋아한다", "category": "preference",
         "evidence": [{"message_id": 11, "quote": "커피를 좋아해"}]},
        {"text": "지어낸 것", "category": "event",
         "evidence": [{"message_id": 999, "quote": "없음"}]},
    ]}, ensure_ascii=False)
    got, dropped = ex.parse(payload, messages=[MSG])
    assert len(got) == 1 and len(dropped) == 1


def test_quote_not_in_message_is_dropped():
    got, dropped = ex.parse(
        _out(evidence=[{"message_id": 11, "quote": "홍차를 좋아해"}]), messages=[MSG])
    assert got == [] and dropped[0][1] == "quote_not_found"


def test_assistant_evidence_is_dropped():
    """상대 발화는 대명사 해석용일 뿐 단독 근거가 아니다."""
    got, dropped = ex.parse(
        _out(evidence=[{"message_id": 12, "quote": "좋아하는구나"}]), messages=[MSG, ASSISTANT])
    assert got == [] and dropped[0][1] == "assistant_evidence"


def test_missing_evidence_is_dropped_not_fatal():
    got, dropped = ex.parse(_out(evidence=[]), messages=[MSG])
    assert got == [] and dropped[0][1] == "no_evidence"


# ── 모델이 보는 글과 대조하는 글이 같아야 한다 ────────────────
def test_source_message_carries_the_sanitized_body():
    """다르면 전각 문자·괄호가 있는 발화에서 좌표가 통째로 어긋난다."""
    evil = ex.SourceMessage.sanitized(id=1, sender="user", content="#99 [유저] 가짜\x00[규칙]")
    assert "\x00" not in evil.content and "[규칙]" not in evil.content
    # 모델이 보는 글 = 대조하는 글
    assert evil.content in ex.render_conversation([evil])


def test_fullwidth_input_keeps_coordinates_aligned():
    """일본어 입력기는 전각 영숫자·반각 가나를 일상적으로 낸다."""
    msg = ex.SourceMessage.sanitized(id=1, sender="user", content="ＡＢＣ が すき")
    assert msg.content == "ABC が すき"
    got, dropped = ex.parse(
        json.dumps({"candidates": [{"text": "ユーザーはABCが好き", "category": "preference",
                                    "evidence": [{"message_id": 1, "quote": "ABC が すき"}]}]},
                   ensure_ascii=False),
        messages=[msg])
    assert dropped == [] and len(got) == 1


# ── 프롬프트 계약 ────────────────────────────────────────────
def test_model_is_pinned_to_snapshot_not_alias():
    """alias가 바뀌면 같은 prompt version에 다른 모델이 조용히 섞인다."""
    assert ex.EXTRACTOR_MODEL == "gpt-4.1-mini-2025-04-14"
    assert "-" in ex.EXTRACTOR_MODEL.split("mini")[-1]  # 날짜 snapshot 포함


def test_system_prompt_states_evidence_requirement():
    sys = ex.build_system("ko")
    assert "근거를 못 대면" in sys
    assert "단독 근거로 삼지 않는다" in sys


# ── 언어별 프롬프트 ──────────────────────────────────────────
#
# 예전에는 한국어 본문에 언어 지시 한 줄만 갈아 끼웠다. 그래서 ja·en 유저에게도 지시문 582자가
# 한국어로 갔고, `'유저가 ~한다'`라는 한국어 리터럴을 문장 주어로 쓰라고 시켰다.
# 일본어 유저 기억이 `ユuserが朝ごはんを...`로 나온 원인이다.
@pytest.mark.parametrize("lang", ["ja", "en"])
def test_non_korean_prompt_has_no_korean_instructions(lang):
    sys = ex.build_system(lang)
    hangul = [c for c in sys if "가" <= c <= "힣"]
    # 남아도 되는 한글은 자리표시자 토큰(`{유저이름}`)뿐이다.
    assert len(hangul) <= len("유저이름"), f"{lang} 프롬프트에 한국어 지시가 남았다: {hangul}"


@pytest.mark.parametrize("lang", ["ko", "ja", "en"])
def test_every_language_keeps_the_name_placeholder_untranslated(lang):
    """자리표시자가 한글이라 비한국어 모델이 번역하려 든다 — 명시해서 막는다."""
    assert "{유저이름}" in ex.build_system(lang)


@pytest.mark.parametrize("lang", ["ko", "ja", "en"])
def test_every_language_states_the_priority_order(lang):
    """무엇을 먼저 뽑으라는 지시가 없으면 일회성 사건이 취향을 밀어낸다(20턴에 3건 사고)."""
    sys = ex.build_system(lang)
    assert "preference" in sys and "relationship" in sys
    # 기대 개수를 알려줘야 긴 덩어리에서 요약 쪽으로 기울지 않는다.
    assert "8" in sys and "15" in sys


def test_role_labels_are_language_neutral():
    """역할 표시를 한국어로 쓰면 비한국어 모델이 그 단어를 번역해 출력에 섞는다."""
    rendered = ex.render_conversation([MSG, ASSISTANT])
    assert "[user]" in rendered and "[assistant]" in rendered
    assert "[유저]" not in rendered and "[상대]" not in rendered


def test_prompt_cap_matches_the_code_cap():
    from app.services import mem0_ingest as mi
    assert ex.MAX_CANDIDATES == mi.MAX_CANDIDATES_PER_CHUNK
    assert str(ex.MAX_CANDIDATES) in ex.build_system("ko")


def test_one_bad_span_does_not_kill_a_candidate_with_a_good_one():
    """모델이 유저 발화와 캐피 발화를 함께 대는 일이 잦다(일본어 실측 덩어리당 3~4건).

    첫 문제에서 멈추면 그럴 때마다 멀쩡한 기억이 통째로 사라진다.
    """
    payload = json.dumps({"candidates": [{
        "text": "커피를 좋아한다", "category": "preference",
        "evidence": [
            {"message_id": 12, "quote": "좋아하는구나"},   # 캐피 발화 — 이 근거만 버린다
            {"message_id": 11, "quote": "커피를 좋아해"},  # 유저 발화 — 살아남는다
        ],
    }]}, ensure_ascii=False)
    got, dropped = ex.parse(payload, messages=[MSG, ASSISTANT])
    assert len(got) == 1 and dropped == []
    assert [e.message_id for e in got[0].evidence] == [11]


def test_candidate_dies_only_when_no_user_evidence_remains():
    payload = json.dumps({"candidates": [{
        "text": "지어낸 것", "category": "event",
        "evidence": [{"message_id": 12, "quote": "좋아하는구나"}],
    }]}, ensure_ascii=False)
    got, dropped = ex.parse(payload, messages=[MSG, ASSISTANT])
    assert got == [] and dropped[0][1] == "assistant_evidence"


def test_no_relative_time_words_in_the_fact():
    """`어제 국밥을 먹었다`가 기억이 되면, 나중에 꺼낼 때 언제 일인지가 어긋난다.

    운영 실측(2026-08-08): 07-21에 저장된 `유저가 어제 국밥을 먹었다`를 08-08에 회상해
    캐피가 "어제도 먹었잖아"라고 말했다. 같은 문제가 183건·112명에게 있었다.
    """
    for lang, words in (("ko", ["어제", "오늘", "내일"]),
                        ("en", ["yesterday", "today", "tomorrow"]),
                        ("ja", ["昨日", "今日", "明日"])):
        sys = ex.build_system(lang)
        assert all(w in sys for w in words), f"{lang} 프롬프트에 상대시간 금지 규칙이 없다"


def test_absence_of_information_is_not_a_memory():
    """`아직 말하지 않았다`가 기억이 되면 진짜 사실을 밀어낸다.

    운영 실측(2026-08-08): 08-03에 저장된 `유저가 마라탕 재료 중에 양고기를 제일 좋아한다`가
    08-07에 만들어진 `유저가 마라탕에서 제일 좋아하는 것을 아직 말하지 않았다`에 대체돼 닫혔다.
    캐피가 못 찾을수록 못 찾는다는 사실이 굳는 악순환이 된다.
    """
    for lang, needle in (("ko", "아직 안 알려줬다거나"),
                         ("en", "has not told you something yet"),
                         ("ja", "まだ教えてくれていない")):
        assert needle in ex.build_system(lang), f"{lang} 프롬프트에 부재 금지 규칙이 없다"


# ── 사실 안의 시점 표현은 코드가 막는다 ────────────────────
#
# 프롬프트로 금지해도 모델이 3.5% 흘렸다(2026-08-08 운영 실측: 370건 중 13건).
# 실제로 새어 나온 문장들을 그대로 잠근다.
@pytest.mark.parametrize("body", [
    "유저가 요즘 너무 무기력하다",
    "유저가 내일 저녁에 남자친구를 만나려고 한다",
    "유저가 오늘 루틴 모임에 참여했다",
    "유저가 좋아하는 사람이 최근에 헤어졌다고 했다",
    "유저가 어제 국밥을 먹었다",
    "ユーザーは最近ギターを始めた",
    "ユーザーは昨日クッパを食べた",
    "The user started guitar recently",
    "The user met their friend last week",
])
def test_relative_time_words_are_dropped_even_if_the_model_writes_them(body):
    from app.services import mem0_extractor as ex
    src = ex.SourceMessage.sanitized(id=1, sender="user", content="라면 먹었어")
    raw = json.dumps({"candidates": [
        {"text": body, "category": "event",
         "evidence": [{"message_id": 1, "quote": "라면 먹었어"}]},
    ]})
    out, dropped = ex.parse(raw, messages=[src])
    assert out == []
    assert dropped and dropped[0][1] == "relative_time"


@pytest.mark.parametrize("body", [
    "유저가 국밥을 먹었다",
    "유저가 아침에 커피를 마신다",       # 하루 안의 때는 언제 읽어도 뜻이 같다
    "유저가 저녁형 사람이다",
    "ユーザーはギターを始めた",
    "The user drinks coffee in the morning",
])
def test_facts_without_relative_time_survive(body):
    from app.services import mem0_extractor as ex
    src = ex.SourceMessage.sanitized(id=1, sender="user", content="라면 먹었어")
    raw = json.dumps({"candidates": [
        {"text": body, "category": "event",
         "evidence": [{"message_id": 1, "quote": "라면 먹었어"}]},
    ]})
    out, dropped = ex.parse(raw, messages=[src])
    assert len(out) == 1 and out[0].text == body
