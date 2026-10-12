"""Development-only synthetic evaluator: exact baseline, bounded wire, no live reads."""
import hashlib
import json

from fastapi.testclient import TestClient
import pytest

from app.config import settings
from app.core.db import get_session
from app.core.security import get_current_user
from app.main import create_app
from app.schemas.dev import ChatEvalRequest, QualityEvalCaseRequest
from app.services import conversation_quality_eval as quality
from app.services import model_eval, prompts
from scripts import eval_conversation_quality as offline

SYNTHETIC_OPERATOR = "00000000-0000-4000-8000-000000000123"


def payload(**overrides):
    _, fixture_hash = offline.load_cases()
    body = {"provider": "openai", "model": "gpt-6-luna", "quality": {
        "case_id": "ko_d01", "turn": 0, "variant": "candidate", "fixture_hash": fixture_hash,
    }}
    body.update(overrides)
    return body


def test_all_languages_reconstruct_exact_pinned_baseline():
    artifact = json.loads((quality.EVALS / "conversation_quality_baseline6fb.json").read_text())
    for locale in ("ko", "ja", "en"):
        assert set(artifact["locales"][locale]["sections"]) == quality.BASELINE_HEADERS[locale]
        actual = quality._baseline(prompts.system_prompt(locale), locale)
        assert hashlib.sha256(actual.encode()).hexdigest() == artifact["locales"][locale]["system_sha256"]


def test_baseline_rejects_different_section_set_even_with_six_entries(monkeypatch):
    from pathlib import Path
    original_read = Path.read_text

    def replaced(path, *args, **kwargs):
        text = original_read(path, *args, **kwargs)
        if path.name == "conversation_quality_baseline6fb.json":
            data = json.loads(text)
            sections = data["locales"]["en"]["sections"]
            sections["Unapproved header"] = sections.pop("Steadiness")
            return json.dumps(data)
        return text

    monkeypatch.setattr(Path, "read_text", replaced)
    with pytest.raises(quality.QualityFixtureError):
        quality._baseline(prompts.system_prompt("en"), "en")


def test_untracked_persona_change_fails_closed_for_both_arms(monkeypatch):
    original = prompts.system_prompt
    monkeypatch.setattr(prompts, "system_prompt", lambda locale: original(locale) + "\nUnpinned change.")
    for variant in ("baseline6fb", "candidate"):
        request = payload()["quality"] | {"variant": variant}
        with pytest.raises(quality.QualityFixtureError):
            quality.build_input(QualityEvalCaseRequest(**request))


def test_clock_mood_and_history_are_server_owned_and_deterministic():
    cases, fixture_hash = offline.load_cases()
    request = QualityEvalCaseRequest(case_id="ko_h04", turn=3, variant="candidate",
                                   mode="fixed_history", fixture_hash=fixture_hash)
    system, messages, metadata = quality.build_input(request)
    assert len(system) > 1000
    assert "[User mood selection] 2026-11-03: unknown" in messages[-2]["content"]
    assert messages[-2]["role"] == "system" and messages[-1]["role"] == "user"
    assert metadata["history_hash"] == offline.digest(offline.fixed_history(cases["ko_h04"]))
    assert metadata["provider_attempts_upper"] == 3
    assert metadata["wire_chars"] <= quality.MAX_WIRE_CHARS


@pytest.mark.parametrize("mutation", [
    {"messages": [{"role": "user", "content": "unapproved context"}]},
    {"language": "ko"}, {"use_persona": True}, {"max_tokens": 1024}, {"provider": "anthropic"},
])
def test_mixed_legacy_and_quality_inputs_are_rejected(mutation):
    with pytest.raises(ValueError):
        ChatEvalRequest(**payload(**mutation))


def test_fixture_ids_history_and_roles_are_bounded():
    valid = payload()["quality"]
    for changes in ({"case_id": "ko_d09"}, {"turn": 4}, {"turn": 1},
                    {"prior_assistant_outputs": ["x" * 4001]},
                    {"user_id": SYNTHETIC_OPERATOR}, {"system": "override"}):
        with pytest.raises(ValueError):
            QualityEvalCaseRequest(**(valid | changes))
    with pytest.raises(ValueError):
        ChatEvalRequest(provider="openai", model="gpt-6-luna")


@pytest.fixture
def dev_client(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "enable_dev_routes", True)
    monkeypatch.setattr(settings, "dev_operator_user_ids", SYNTHETIC_OPERATOR)
    monkeypatch.setattr(settings, "model_chat", "gpt-6-luna")
    monkeypatch.setattr(settings, "openai_api_key", "synthetic-test-placeholder")
    monkeypatch.setattr(settings, "revenuecat_webhook_auth", "synthetic-test-placeholder")
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: SYNTHETIC_OPERATOR

    async def forbidden_session():
        raise AssertionError("quality mode must not request a live DB session")
        yield  # pragma: no cover

    app.dependency_overrides[get_session] = forbidden_session
    calls = []

    async def fake(provider, model, system, messages, **kwargs):
        calls.append((system, messages, kwargs))
        return model_eval.EvalResult(provider, model, "합성 응답", 1, 20, 3, 0.01,
                                    model_snapshot="synthetic-snapshot")

    monkeypatch.setattr(model_eval, "run_eval", fake)
    yield TestClient(app), app, calls
    app.dependency_overrides.clear()


def test_quality_route_has_no_live_data_dependency_and_keeps_adapter(dev_client):
    client, _, calls = dev_client
    response = client.post("/dev/chat-eval", json=payload())
    assert response.status_code == 200
    assert response.json()["quality"]["scope"] == "source_prompt"
    assert calls[0][2] == {"max_tokens": 384, "redact_errors": True}


def test_auth_operator_and_environment_guards(dev_client, monkeypatch):
    client, app, calls = dev_client
    monkeypatch.setattr(settings, "environment", "local")
    assert client.post("/dev/chat-eval", json=payload()).status_code == 403
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "dev_operator_user_ids", "")
    assert client.post("/dev/chat-eval", json=payload()).status_code == 403
    app.dependency_overrides.pop(get_current_user)
    assert client.post("/dev/chat-eval", json=payload()).status_code == 401
    assert calls == []


def test_mismatch_422_and_hash_failure_503_do_not_call_model(dev_client):
    client, _, calls = dev_client
    assert client.post("/dev/chat-eval", json=payload(language="ko")).status_code == 422
    body = payload()
    body["quality"]["fixture_hash"] = "0" * 64
    result = client.post("/dev/chat-eval", json=body)
    assert result.status_code == 503
    assert calls == []


async def test_quality_error_logging_does_not_print_exception_contents(monkeypatch, caplog):
    async def fail(*args):
        raise RuntimeError("SYNTHETIC_SECRET_SENTINEL")
    monkeypatch.setitem(model_eval._ADAPTERS, "openai", fail)
    result = await model_eval.run_eval("openai", "gpt-6-luna", "synthetic", [], redact_errors=True)
    assert result.error == "RuntimeError"
    assert "SYNTHETIC_SECRET_SENTINEL" not in caplog.text


def test_docker_packages_only_named_quality_fixtures():
    docker = (quality.EVALS.parent / "Dockerfile").read_text()
    for filename in ("conversation_quality_v1.json", "conversation_quality_holdout_v1.json",
                     "conversation_quality_autonomy_v1.json", "conversation_quality_baseline6fb.json",
                     "conversation_quality_mood_probe_v1.json"):
        assert f"evals/{filename}" in docker


def test_autonomy_probes_are_fixed_history_and_have_the_same_wire_contract():
    cases, fixture_hash = offline.load_cases()
    for locale in ("ko", "ja", "en"):
        for index in (1, 2, 3):
            case_id = f"{locale}_a0{index}"
            with pytest.raises(ValueError):
                QualityEvalCaseRequest(case_id=case_id, turn=0, variant="candidate", fixture_hash=fixture_hash)
            request = QualityEvalCaseRequest(case_id=case_id, turn=3, variant="candidate",
                                            mode="fixed_history", fixture_hash=fixture_hash)
            _, messages, metadata = quality.build_input(request)
            assert cases[case_id]["turns"][3]["text"] in messages[-1]["content"]
            assert metadata["history_hash"] == offline.digest(offline.fixed_history(cases[case_id]))


def test_mood_probe_bundle_preserves_original_hash_and_is_one_turn_only():
    _, original_hash = quality._fixtures()
    assert original_hash == "2c754799983c7140358d8e2cfc71e65c3a6f421cf104b78b4dc9bec9c2567a13"
    cases, fixture_hash = offline.load_stage_cases("mood_probe")
    assert quality._fixtures(mood_probe=True)[1] == fixture_hash != original_hash
    assert set(cases) == quality.MOOD_CASE_IDS
    for case_id, case in cases.items():
        request = dict(case_id=case_id, turn=0, variant="candidate",
                       mode="fixed_history", fixture_hash=fixture_hash)
        _, messages, metadata = quality.build_input(QualityEvalCaseRequest(**request))
        assert [m["role"] for m in messages] == ["system", "user"]
        assert case["turns"][0]["text"] in messages[-1]["content"]
        assert metadata["history_hash"] == offline.digest(offline.fixed_history(case))
        for invalid in ({"turn": 3}, {"mode": "rollout"}, {"prior_assistant_outputs": ["unapproved"]}):
            with pytest.raises(ValueError):
                QualityEvalCaseRequest(**(request | invalid))
        with pytest.raises(quality.QualityFixtureError):
            quality.build_input(QualityEvalCaseRequest(**(request | {"fixture_hash": original_hash})))
    # The one-turn exception never applies to established fixed-history cases.
    with pytest.raises(ValueError):
        QualityEvalCaseRequest(case_id="ko_d01", turn=0, mode="fixed_history",
                              variant="candidate", fixture_hash=original_hash)


def test_missing_mood_bundle_fails_closed_without_affecting_old_cases(monkeypatch):
    from pathlib import Path
    original_read = Path.read_text

    def missing(path, *args, **kwargs):
        if path.name == "conversation_quality_mood_probe_v1.json":
            raise FileNotFoundError("synthetic missing fixture")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", missing)
    with pytest.raises(quality.QualityFixtureError):
        quality._fixtures(mood_probe=True)
    assert len(quality._fixtures()[0]) == 45


def test_mood_route_preserves_no_live_reads_and_fixed_adapter(dev_client):
    client, _, calls = dev_client
    _, fixture_hash = offline.load_stage_cases("mood_probe")
    body = payload(quality={"case_id": "ko_m01", "turn": 0, "mode": "fixed_history",
                            "variant": "candidate", "fixture_hash": fixture_hash})
    result = client.post("/dev/chat-eval", json=body)
    assert result.status_code == 200 and len(calls) == 1
    assert calls[0][2] == {"max_tokens": 384, "redact_errors": True}
    assert len(calls[0][1]) == 2
