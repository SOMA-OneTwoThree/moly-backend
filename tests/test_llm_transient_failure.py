"""Provider failures are classified without changing inference or importing SDKs."""
import asyncio
import builtins
import importlib
import sys

import httpx
import pytest

from app.core.errors import AppError
from app.services import llm


@pytest.mark.parametrize("provider", ("openai", "anthropic"))
@pytest.mark.parametrize("status,expected", (
    (400, False), (401, False), (403, False), (404, False), (422, False),
    (408, True), (409, True), (429, True), (500, True), (502, True), (503, True),
))
def test_sdk_status_failure_classification(provider, status, expected):
    sdk = importlib.import_module(provider)
    response = httpx.Response(status, request=httpx.Request("POST", "https://provider.invalid"))
    failure = sdk.APIStatusError("private provider body", response=response, body=None)
    assert llm.is_transient_failure(failure) is expected


@pytest.mark.parametrize("provider", ("openai", "anthropic"))
@pytest.mark.parametrize("kind", ("APIConnectionError", "APITimeoutError"))
def test_sdk_connection_and_timeout_are_transient(provider, kind):
    sdk = importlib.import_module(provider)
    failure = getattr(sdk, kind)(request=httpx.Request("POST", "https://provider.invalid"))
    assert llm.is_transient_failure(failure)


@pytest.mark.parametrize("failure,expected", (
    (TimeoutError("deadline"), True),
    (ValueError("application bug"), False),
    (RuntimeError("application bug"), False),
    (asyncio.CancelledError(), False),
    (AppError("LIMIT", 429, "application limit"), False),
))
def test_non_sdk_exception_classification(failure, expected):
    assert llm.is_transient_failure(failure) is expected


def test_classifier_does_not_import_absent_sdks(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name not in ("openai", "anthropic")
        return original_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "openai", raising=False)
    monkeypatch.delitem(sys.modules, "anthropic", raising=False)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert llm.is_transient_failure(RuntimeError("not a provider exception")) is False
    assert llm.is_transient_failure(TimeoutError()) is True
