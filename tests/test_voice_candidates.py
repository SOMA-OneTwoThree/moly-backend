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
    )
    for language, candidate in (("ja", "voice-free-ko-v5"), ("fr", "c0"), ("ko", "unknown")):
        with pytest.raises(ValueError):
            prompts.eval_system_prompt(language, candidate)
