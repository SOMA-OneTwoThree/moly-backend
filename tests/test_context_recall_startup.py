"""Local-only startup preparation: no provider, vector or DB request."""
from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core import db as app_db
from app.services import chat, llm, memory_embeddings, usage_ledger


async def wait_thread_event(event):
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(0.001)


@pytest.fixture
def local_factories(monkeypatch):
    monkeypatch.setattr(chat.settings, "openai_api_key", "synthetic-startup-key")
    forbidden = Mock(side_effect=AssertionError("startup cannot request data"))
    calls = []
    resource = SimpleNamespace(create=forbidden)
    collection = SimpleNamespace(query=forbidden, upsert=forbidden, fetch=forbidden)

    class Client:
        @property
        def embeddings(self):
            calls.append(("embeddings", threading.get_ident()))
            return resource

    class Index:
        def _col(self):
            calls.append(("collection", threading.get_ident()))
            return collection

    client, index = Client(), Index()
    sdk = Mock(return_value=client)
    adapter = Mock(return_value=index)
    monkeypatch.setattr(llm, "_get_openai_client", sdk)
    monkeypatch.setattr(chat, "_recall_adapter", adapter)
    monkeypatch.setattr(app_db, "get_sessionmaker", forbidden)
    monkeypatch.setattr(memory_embeddings, "embed_texts", AsyncMock(side_effect=AssertionError("no embedding call")))
    monkeypatch.setattr(llm, "generate", AsyncMock(side_effect=AssertionError("no model call")))
    return SimpleNamespace(calls=calls, forbidden=forbidden, client=client, index=index,
                           sdk=sdk, adapter=adapter)


async def test_no_key_skips_all_constructors(local_factories, monkeypatch):
    monkeypatch.setattr(chat.settings, "openai_api_key", "")
    await chat.prepare_recall_clients()
    local_factories.sdk.assert_not_called()
    local_factories.adapter.assert_not_called()
    local_factories.forbidden.assert_not_called()


async def test_only_local_objects_are_built_off_main_loop(local_factories, caplog):
    main_thread = threading.get_ident()
    with caplog.at_level(logging.INFO, logger=chat._log.name):
        await chat.prepare_recall_clients()
    assert [name for name, _ in local_factories.calls] == ["embeddings", "collection"]
    assert all(thread != main_thread for _, thread in local_factories.calls)
    local_factories.sdk.assert_called_once_with()
    local_factories.adapter.assert_called_once_with()
    local_factories.forbidden.assert_not_called()
    memory_embeddings.embed_texts.assert_not_awaited()
    llm.generate.assert_not_awaited()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("context_recall_startup ")]
    assert len(lines) == 2
    assert all("state=ready" in line and "error_type=none" in line for line in lines)
    assert all("synthetic-startup-key" not in line for line in lines)


@pytest.mark.parametrize("failed", ["sdk", "adapter"])
async def test_component_failure_preserves_other_local_preparation(local_factories, caplog, failed):
    secret = "synthetic-secret-and-source-never-log"
    getattr(local_factories, failed).side_effect = RuntimeError(secret)
    with caplog.at_level(logging.INFO, logger=chat._log.name):
        await chat.prepare_recall_clients()
    local_factories.sdk.assert_called_once_with()
    local_factories.adapter.assert_called_once_with()
    local_factories.forbidden.assert_not_called()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("context_recall_startup ")]
    assert len(lines) == 2
    assert sum("state=unavailable" in line and "error_type=RuntimeError" in line for line in lines) == 1
    assert sum("state=ready" in line for line in lines) == 1
    assert all(secret not in line and "synthetic-startup-key" not in line for line in lines)


async def test_startup_log_handler_failure_does_not_fail_preparation(local_factories):
    observed = []

    class FailedHandler(logging.Handler):
        def emit(self, record):
            if record.getMessage().startswith("context_recall_startup "):
                observed.append(record.getMessage())
                raise RuntimeError("synthetic log serializer failure")

    handler = FailedHandler()
    prior = chat._log.level
    chat._log.setLevel(logging.INFO)
    chat._log.addHandler(handler)
    try:
        await chat.prepare_recall_clients()
    finally:
        chat._log.removeHandler(handler)
        chat._log.setLevel(prior)
    assert len(observed) == 2
    assert [name for name, _ in local_factories.calls] == ["embeddings", "collection"]
    local_factories.forbidden.assert_not_called()


async def test_cancellation_joins_initialization_before_propagating(local_factories):
    started, release, completed = threading.Event(), threading.Event(), threading.Event()

    def slow_sdk():
        started.set()
        if not release.wait(3):
            raise AssertionError("test release missing")
        completed.set()
        return local_factories.client

    local_factories.sdk.side_effect = slow_sdk
    pending = asyncio.create_task(chat.prepare_recall_clients())
    try:
        await wait_thread_event(started)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()  # Repeated startup cancellation cannot abandon the thread.
        await asyncio.sleep(0)
        assert not pending.done() and not completed.is_set()
        local_factories.adapter.assert_not_called()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert completed.is_set()
    local_factories.adapter.assert_called_once_with()
    local_factories.forbidden.assert_not_called()


@pytest.mark.parametrize("cancel_startup", [False, True])
async def test_lifespan_cannot_start_flusher_or_yield_before_preparation_finishes(
    local_factories, monkeypatch, cancel_startup,
):
    from app.main import create_app
    from app.services import crisis_chat, training_catalog, chat_content_encryption
    monkeypatch.setattr(chat_content_encryption, "prepare_runtime", AsyncMock())
    from app.services.banner_catalog import BannerCatalog
    from app.services.topic_catalog import TopicCatalog

    monkeypatch.setattr(BannerCatalog, "load", lambda: object())
    monkeypatch.setattr(TopicCatalog, "load", lambda: object())
    monkeypatch.setattr(training_catalog, "current", lambda: None)
    drained = AsyncMock()
    monkeypatch.setattr(crisis_chat, "drain_cost_tasks", drained)
    lifecycle = []

    async def flusher(stop):
        lifecycle.append("flusher_started")
        await stop.wait()
        lifecycle.append("flusher_stopped")

    monkeypatch.setattr(usage_ledger, "run_close_flusher", flusher)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocking_sdk():
        started.set()
        if not release.wait(3):
            raise AssertionError("test release missing")
        finished.set()
        return local_factories.client

    local_factories.sdk.side_effect = blocking_sdk
    app = create_app()
    entered, exit_requested = asyncio.Event(), asyncio.Event()

    async def serve():
        async with app.router.lifespan_context(app):
            lifecycle.append("yielded")
            entered.set()
            await exit_requested.wait()

    pending = asyncio.create_task(serve())
    try:
        await wait_thread_event(started)
        assert not entered.is_set() and lifecycle == []
        if cancel_startup:
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done() and not finished.is_set()
        release.set()
        if cancel_startup:
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert not entered.is_set() and lifecycle == []
            drained.assert_not_awaited()
        else:
            async with asyncio.timeout(2):
                await entered.wait()
            assert finished.is_set()
            exit_requested.set()
            await pending
            assert "yielded" in lifecycle
            assert "flusher_started" in lifecycle and "flusher_stopped" in lifecycle
            drained.assert_awaited_once_with()
    finally:
        release.set()
        exit_requested.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    assert finished.is_set()
    local_factories.forbidden.assert_not_called()
