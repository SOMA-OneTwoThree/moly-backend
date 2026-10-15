"""Native server prose and fixed development-only locale persona prefixes."""
import hashlib

import pytest

from app.services import chat, naming
from app.services.prompts import eval_system_prompt, system_prompt


@pytest.fixture(autouse=True)
def _local_environment(monkeypatch):
    monkeypatch.setattr(chat.settings, "environment", "local")


@pytest.mark.parametrize("nickname,copula", [("승민", "승민이야"), ("지호", "지호야")])
def test_korean_mutable_block_is_byte_preserved(nickname, copula):
    lead = ["왔네. {유저이름}아", "원문 猫 café…"]
    history = ["2026-10-14T01:00:00+09:00 原文 {유저이름}아"]
    contract = "합의 {유저이름}아"
    relationship = "relationship 原文"
    expected = "\n\n".join([
        f"[상대]\n지금 얘기하는 사람 이름은 {copula}.",
        "[먼저 건넨 말]\n이 대화 직전에 네가 먼저 말을 걸었어. 상대는 그걸 보고 답한 거야. "
        "같은 인사를 또 하지 마.\n" + "\n".join(naming.render(item, nickname) for item in lead),
        "[Past Cappy messages]\n" + naming.render("\n".join(history), nickname),
        naming.render(contract, nickname), relationship,
    ])
    assert chat._build_system(
        "ko", nickname, lead, historical_lead=history, contract_text=contract,
        relationship_v2=relationship,
    ) == [system_prompt("ko"), expected]


@pytest.mark.parametrize("locale,name_header,lead_header,history_header", [
    ("ko", "[상대]", "[먼저 건넨 말]", "[Past Cappy messages]"),
    ("ja", "[相手]", "[先にかけた言葉]", "[過去のキャピーの発言]"),
    ("en", "[Conversation partner]", "[Your opening message]", "[Past Cappy messages]"),
])
def test_all_locales_keep_values_and_block_order(locale, name_header, lead_header, history_header):
    nickname = "Alex"
    lead = ["원문1 {유저이름} 猫…", "original2 café"]
    history = ["2026-10-14T01:02:03+09:00 history {유저이름} 猫"]
    contract = "agreement {유저이름}"
    relationship = "relationship native 原文"
    blocks = chat._build_system(
        locale, nickname, lead, historical_lead=history, contract_text=contract,
        relationship_v2=relationship,
    )
    assert len(blocks) == 2 and blocks[0] == system_prompt(locale)
    dynamic = blocks[1]
    assert dynamic.index(name_header) < dynamic.index(lead_header) < dynamic.index(history_header)
    assert "\n".join(naming.render(item, nickname) for item in lead) in dynamic
    assert naming.render(history[0], nickname) in dynamic
    assert dynamic.endswith(naming.render(contract, nickname) + "\n\n" + relationship)
    assert naming.TOKEN not in dynamic
    if locale != "ko":
        prose = dynamic.split("원문1")[0]
        assert not any("가" <= character <= "힣" for character in prose)
        assert nickname in prose and "Alex야" not in prose


@pytest.mark.parametrize("locale", ["ko", "ja", "en"])
def test_repeated_inputs_preserve_static_prefix_and_dynamic_stability(locale):
    first = chat._build_system(locale, "Alex", ["original 猫"])
    assert first == chat._build_system(locale, "Alex", ["original 猫"])
    changed = chat._build_system(locale, "Nari", ["changed 原文"])
    assert first[0] == changed[0] == system_prompt(locale)
    assert first[1] != changed[1]


@pytest.mark.parametrize("locale", ["fr", "zh-Hant-TW", "unknown", "en-US"])
def test_unsupported_or_regional_language_uses_canonical_english(locale):
    assert chat._build_system(locale, "Alex", ["original"], historical_lead=["past"]) == (
        chat._build_system("en", "Alex", ["original"], historical_lead=["past"])
    )


@pytest.mark.parametrize("locale", ["ko", "ja", "en"])
def test_no_values_adds_no_dynamic_block(locale):
    assert chat._build_system(locale, None, []) == [system_prompt(locale)]


@pytest.mark.parametrize("environment", ["production", "local", "test"])
@pytest.mark.parametrize("locale", ["ko", "ja", "en", "ko-KR", "ja-JP", "en-US", "fr"])
def test_non_development_persona_remains_baseline(monkeypatch, environment, locale):
    monkeypatch.setattr(chat.settings, "environment", environment)
    assert chat._build_system(locale, None)[0] == system_prompt(locale)


@pytest.mark.parametrize("locale", ["ko", "ko-KR"])
def test_development_korean_selects_structured_v9(monkeypatch, locale):
    monkeypatch.setattr(chat.settings, "environment", "development")
    assert chat._build_system(locale, None)[0] == eval_system_prompt("ko", "voice-structured-ko-v9")[0]


@pytest.mark.parametrize("locale,canonical,digest", [
    ("ja", "ja", "738a3c95e74983927fcdfb887ba7094c0de17a44f2608db0a24f8533a55bc599"),
    ("ja-JP", "ja", "738a3c95e74983927fcdfb887ba7094c0de17a44f2608db0a24f8533a55bc599"),
    ("en", "en", "10452aadab1a3e3ed056bf9d2969a336f1d2e36aac9814890daa9ff063ac5d2a"),
    ("en-US", "en", "10452aadab1a3e3ed056bf9d2969a336f1d2e36aac9814890daa9ff063ac5d2a"),
    ("fr", "en", "10452aadab1a3e3ed056bf9d2969a336f1d2e36aac9814890daa9ff063ac5d2a"),
])
def test_development_selects_exact_fixed_candidate(monkeypatch, locale, canonical, digest):
    monkeypatch.setattr(chat.settings, "environment", "development")
    persona = chat._build_system(locale, None)[0]
    assert persona == eval_system_prompt(canonical, "voice-combined-v3")[0]
    assert hashlib.sha256(persona.encode()).hexdigest() == digest
    assert persona != system_prompt(locale)


@pytest.mark.parametrize("locale", ["ko", "ja", "en", "ja-JP", "fr"])
def test_development_changes_only_the_first_prefix_block(monkeypatch, locale):
    values = {
        "historical_lead": ["2026-10-14T01:02:03+09:00 原文 {유저이름}"],
        "contract_text": "agreement {유저이름}",
        "relationship_v2": "[관계]\n함께한 날: 2일" if locale == "ko" else "relationship original",
        "summary": "volatile summary",
        "current_state": "volatile current state",
    }
    baseline = chat._build_system(locale, "Alex", ["original 猫 café…"], **values)
    monkeypatch.setattr(chat.settings, "environment", "development")
    candidate = chat._build_system(locale, "Alex", ["original 猫 café…"], **values)
    assert baseline[1:] == candidate[1:]
    assert "volatile summary" not in "\n".join(candidate)
    assert "volatile current state" not in "\n".join(candidate)
    changed = chat._build_system(locale, "Nari", ["changed lead"], **values)
    assert candidate[0] == changed[0]
