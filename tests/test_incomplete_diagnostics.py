from dataclasses import replace
import json

import pytest

from app.services import incomplete_diagnostics as diag, llm
from app.services.agent.runtime import _require_completed_step
from tests.test_agent_runtime import _text_step


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(diag, "_remaining", 100)
    monkeypatch.setattr(diag.time, "time", lambda: diag._EXPIRES_AT - 1)


@pytest.mark.parametrize("finish,text,rejected,reason", [
    ("length", "private partial reply", 0, "unfinished"),
    ("content_filter", None, 0, "unfinished"),
    ("stop", "", 0, "blank_text"),
    ("tool_calls", None, 1, "rejected_final"),
    ("private-provider-value", None, 0, "unfinished"),
])
def test_rejection_metadata_distinguishes_causes_without_content(finish, text, rejected, reason):
    step = replace(_text_step(text), finish_reason=finish,
                   raw_tool_calls=rejected, rejected_final_responses=rejected)
    with pytest.raises(llm.IncompleteResponseError) as caught:
        _require_completed_step(step, max_tokens=192)
    encoded = diag.suffix(caught.value)
    data = json.loads(encoded.split("=", 1)[1])
    assert data["reason"] == reason
    assert data["max_tokens"] == 192
    assert data["output_tokens"] == step.usage.output_tokens
    assert data["rejected_final_responses"] == rejected
    assert "private" not in encoded


def test_expiry_and_sample_limit_do_not_extend_on_restart(monkeypatch):
    exc = llm.IncompleteResponseError("private")
    exc.diagnostic = {"reason": "unfinished", "finish": "length", "stage": "first"}
    assert all(diag.suffix(exc) for _ in range(100))
    assert diag.suffix(exc) == ""
    monkeypatch.setattr(diag, "_remaining", 100)
    monkeypatch.setattr(diag.time, "time", lambda: diag._EXPIRES_AT)
    assert diag.suffix(exc) == ""


def test_allowlist_and_size_bound():
    exc = llm.IncompleteResponseError("private")
    exc.diagnostic = {"reason": "x" * 10000, "finish": ["private"],
                      "user_id": "private", "text": "private", "output_tokens": 10**100}
    encoded = diag.suffix(exc)
    assert len(encoded) < 400
    assert "private" not in encoded
    assert "1000000" in encoded
    assert diag.suffix(ValueError("private")) == ""
    assert diag.suffix(llm.IncompleteResponseError("private")) == ""


def test_completed_response_has_no_diagnostic():
    assert _require_completed_step(_text_step("valid"), max_tokens=192) is None


def test_invalid_structured_text_preserves_original_error():
    step = replace(_text_step(None), finish_reason="tool_calls",
                   final_response=llm.FinalResponse(text=None, response_mode="summary"))
    with pytest.raises(llm.IncompleteResponseError) as caught:
        _require_completed_step(step, max_tokens=192)
    assert caught.value.diagnostic["final_text_chars"] == 0
    assert caught.value.diagnostic["reason"] == "blank_text"
