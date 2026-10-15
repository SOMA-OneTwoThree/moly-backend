"""Retained development personas preserve baseline facts and fixed prompt bytes."""

from __future__ import annotations

import hashlib

import pytest

from app.services import prompts


def test_v5_baseline_control_is_exact_c0_and_free_persona_has_no_inherited_style_or_footer():
    base, base_version = prompts.eval_system_prompt("ko", "voice-base-ko-v5")
    free, free_version = prompts.eval_system_prompt("ko", "voice-free-ko-v5")
    assert base == prompts.system_prompt("ko")
    assert hashlib.sha256(base.encode()).hexdigest() == (
        "b97bab3e26bd06c6b481102e3a509e182712d850a4e44c0590d1c75e7351a38f"
    )
    assert base_version == free_version == "cappy-voice-20261014-v5"
    assert free == prompts._VOICE_FREE_KO_V5
    assert hashlib.sha256(free.encode()).hexdigest() == (
        "6d2ea1ae2b7b1d0581fb20b071064d8316903c33e7994d1053e9ecd90c8bd3a1"
    )
    assert len(free) < len(base)
    for old in (
        "[관계]",
        "느긋",
        "수줍",
        "여운",
        "짧은 호흡",
        "문을 열어",
        "온기가",
        "[언어]",
        "[물음표]",
        prompts._OUTPUT_CHECK,
        "-네",
        "-구나",
        "-겠다",
    ):
        assert old not in free
    assert "코딩" not in free and "숙제" not in free and "번역" not in free
    for candidate in ("voice-base-ko-v5", "voice-free-ko-v5"):
        for language in ("ja", "en", "ko-KR"):
            with pytest.raises(ValueError):
                prompts.eval_system_prompt(language, candidate)


def test_v5_explicit_fact_and_safety_contract_is_bounded_to_supplied_information():
    free, _ = prompts.eval_system_prompt("ko", "voice-free-ko-v5")
    for contract in (
        "[User mood selection]은 상대가 오늘 앱에서 고른 이름만 담은 내부 정보야.",
        "상대가 말하지 않은 감정·바람·의도를 사실로 단정하지 마.",
        "제공되지 않은 일기 내용도 지어내지 마.",
        "제공된 현재 상태에 네 모습 항목이 없으면 아무것도 걸치지 않은 상태야.",
        "당장 위험하면 현재 안전을 확보하고 가까운 사람이나 현지 긴급 도움을 받을 수 있도록 도와.",
        "다른 화제로 넘어갔다는 이유만으로 확인된 위험이 끝났다고 단정하지 마.",
        "unknown은 선택값을 모른다는 뜻이고 기록 유무의 증거가 아니야.",
    ):
        assert contract in free
    assert "기관 안내나 전화번호를" not in free


@pytest.mark.parametrize(
    "language,candidate,digest",
    [
        ("ko", "c0", "b97bab3e26bd06c6b481102e3a509e182712d850a4e44c0590d1c75e7351a38f"),
        ("ja", "c0", "b8ec8365c7680907d011899e383676ab86ef9db5f3bd98111e376369ead7f1fc"),
        ("en", "c0", "ba9193df404b6fabe192c726613fab1a685533c4815bd5f5b52fedaf9829ecca"),
        (
            "ko",
            "voice-combined-v3",
            "dc420452352b663a24466db1c4d6c40e16e07e58f00d9212e9232ef3dcbe838e",
        ),
        (
            "ja",
            "voice-combined-v3",
            "738a3c95e74983927fcdfb887ba7094c0de17a44f2608db0a24f8533a55bc599",
        ),
        (
            "en",
            "voice-combined-v3",
            "10452aadab1a3e3ed056bf9d2969a336f1d2e36aac9814890daa9ff063ac5d2a",
        ),
    ],
)
def test_retained_persona_hashes_and_protected_spans_are_unchanged(language, candidate, digest):
    system, version = prompts.eval_system_prompt(language, candidate)
    assert hashlib.sha256(system.encode()).hexdigest() == digest
    assert version == (
        "cappy-voice-20261014-v1" if candidate == "c0" else "cappy-voice-20261014-v3"
    )
    baseline = prompts.system_prompt(language)
    for old, new in prompts._eval_voice_replacements(language, candidate):
        assert baseline.count(old) == 1 and system.count(new) == 1
        baseline = baseline.replace(old, "", 1)
        system = system.replace(new, "", 1)
    assert baseline == system


def test_retained_registry_has_no_inactive_alias_or_unregistered_fallback():
    assert prompts.EVAL_PROMPT_CANDIDATE_IDS == (
        "c0",
        "voice-combined-v3",
        "voice-base-ko-v5",
        "voice-free-ko-v5",
        "voice-conversation-ko-v6",
        "voice-structured-ko-v7",
    )
    for language, candidate in (("ja", "voice-free-ko-v5"), ("fr", "c0"), ("ko", "unknown")):
        with pytest.raises(ValueError):
            prompts.eval_system_prompt(language, candidate)


def test_conversation_candidate_replaces_only_the_conversation_section():
    baseline, baseline_version = prompts.eval_system_prompt("ko", "voice-free-ko-v5")
    candidate, candidate_version = prompts.eval_system_prompt("ko", "voice-conversation-ko-v6")
    before, old_conversation = baseline.split("[지금 대화]", 1)
    _, protected = old_conversation.split("[사실과 기능]", 1)
    assert candidate == (
        before + prompts._VOICE_CONVERSATION_KO_V6 + "\n\n[사실과 기능]" + protected
    )
    assert candidate_version == "cappy-voice-20261015-v6"
    assert baseline_version == "cappy-voice-20261014-v5"
    assert hashlib.sha256(candidate.encode()).hexdigest() == (
        "4f209e309a295d450d526e78bc301eeb9440b9fb389fd039cc9353bb15ae5ee0"
    )
    assert candidate != baseline
    assert candidate.count("[지금 대화]") == 1


@pytest.mark.parametrize("language", ["ja", "en", "ko-KR"])
def test_conversation_candidate_cannot_change_other_locale_prompts(language):
    with pytest.raises(ValueError, match="Korean only"):
        prompts.eval_system_prompt(language, "voice-conversation-ko-v6")


@pytest.mark.parametrize("boundary", ["[지금 대화]", "[사실과 기능]"])
def test_conversation_candidate_rejects_changed_source_boundaries(monkeypatch, boundary):
    monkeypatch.setattr(prompts, "_VOICE_FREE_KO_V5", prompts._VOICE_FREE_KO_V5 + boundary)
    with pytest.raises(ValueError, match="boundary changed"):
        prompts.eval_system_prompt("ko", "voice-conversation-ko-v6")


def test_structured_candidate_has_fixed_identity_and_separate_information_permissions():
    system, version = prompts.eval_system_prompt("ko", "voice-structured-ko-v7")
    assert system == prompts._VOICE_STRUCTURED_KO_V7
    assert version == "cappy-voice-20261015-v7"
    assert hashlib.sha256(system.encode()).hexdigest() == (
        "dd7a69992c95bd8ef380673c64738dc59425f51f59dfd4c66daee2268c2cb0ca"
    )
    headings = (
        "[정체성]", "[성격]", "[말하는 방식]", "[정보 사용 기준]", "[기본 취향]",
        "[사용자 사실과 기억]", "[서버 상태와 도구]", "[시간과 기억]",
        "[앱에서 선택한 감정]", "[운세]", "[안전과 정체성]",
    )
    assert all(system.count(heading) == 1 for heading in headings)
    assert [system.index(heading) for heading in headings] == sorted(
        system.index(heading) for heading in headings
    )
    assert "클래식 엘피" not in system.split("[기본 취향]", 1)[0]


def test_structured_candidate_preserves_all_supplied_fact_and_safety_contracts():
    baseline = prompts._VOICE_FREE_KO_V5
    system, _ = prompts.eval_system_prompt("ko", "voice-structured-ko-v7")
    section_boundaries = (
        ("[말하기]", "[지금 대화]"),
        ("[사실과 기능]", "[시간과 기억]"),
        ("[시간과 기억]", "[앱에서 선택한 감정]"),
        ("[앱에서 선택한 감정]", "[운세]"),
        ("[운세]", "[안전과 정체성]"),
    )
    for start, end in section_boundaries:
        original = baseline.split(start + "\n", 1)[1].split("\n\n" + end, 1)[0]
        # The facts block is deliberately split into user provenance, media and server state.
        for paragraph in original.split("\n"):
            assert paragraph in system
    assert baseline.split("[안전과 정체성]\n", 1)[1] in system


def test_structured_candidate_distinguishes_absent_state_from_empty_appearance():
    system, _ = prompts.eval_system_prompt("ko", "voice-structured-ko-v7")
    state = system.split("[서버 상태와 도구]\n", 1)[1].split("\n\n[시간과 기억]", 1)[0]
    assert "제공된 현재 상태에 네 모습 항목이 없으면 아무것도 걸치지 않은 상태야." in state
    assert "현재 상태 블록 자체가 없으면 네 모습이나 현재 활동은 모르는 범위야." in state
    # These are prompt contracts, not a claim that the model passed a behavioural test.


def test_structured_candidate_gates_background_tastes_without_banning_requested_analysis():
    system, _ = prompts.eval_system_prompt("ko", "voice-structured-ko-v7")
    tastes = system.split("[기본 취향]\n", 1)[1].split("\n\n[사용자 사실과 기억]", 1)[0]
    assert "캐피의 취향을 직접 묻거나 그 항목을 캐피가 좋아하는지 물을 때만 답에 써." in tastes
    assert "그 항목 자체를 꺼냈을 때" not in tastes
    assert "네 의견·가정적 선택·바람은 말해도 돼." in tastes
    assert "설명·감상·분석·확인·요약·인용이나 자기 행동의 평가를 부탁하면 그 요청에 답해." in system
    assert "짧은 수락이나 네 느낌은 말해도 돼." in system


@pytest.mark.parametrize("language", ["ja", "en", "ko-KR", ""])
def test_structured_candidate_is_korean_only(language):
    with pytest.raises(ValueError, match="Korean only"):
        prompts.eval_system_prompt(language, "voice-structured-ko-v7")


@pytest.mark.parametrize("candidate", ["voice-structured-ko-v70", "voice-structured-ko-v7-custom"])
def test_structured_candidate_has_no_unregistered_alias(candidate):
    with pytest.raises(ValueError, match="Unknown evaluation prompt candidate"):
        prompts.eval_system_prompt("ko", candidate)
