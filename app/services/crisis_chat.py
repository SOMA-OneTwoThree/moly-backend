"""Decisions와 답변 초안을 병렬 실행하고 최종 한 경로만 선택한다."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
import logging
import math
import time

from app.config import settings
from app.services import crisis_guard, llm, usage_ledger
from app.services.agent.runtime import AgentTurn

_log = logging.getLogger("moly-backend")
_cost_tasks: set[asyncio.Task] = set()


def _finish_cost(awaitable: Awaitable) -> None:
    task = asyncio.create_task(awaitable)
    _cost_tasks.add(task)

    def completed(done):
        _cost_tasks.discard(done)
        if not done.cancelled():
            try:
                done.result()
            except Exception as exc:
                _log.warning("crisis_cost_close error=%s", type(exc).__name__)
    task.add_done_callback(completed)


async def drain_cost_tasks(timeout_s: float = 1.0) -> None:
    if _cost_tasks:
        _, pending = await asyncio.wait(tuple(_cost_tasks), timeout=timeout_s)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

CONFIG_KEYS = (
    "crisis_guard_enabled", "crisis_guard_timeout_s", "crisis_guard_threshold",
    "crisis_guard_history_messages", "crisis_guard_ready_users",
)


@dataclass(frozen=True)
class GuardConfig:
    enabled: bool = False
    timeout_s: float = 2.0
    threshold: float = 0.4
    history_messages: int = 6


def effective_config(raw: Mapping) -> GuardConfig:
    def bounded(name: str, default: float, low: float, high: float) -> float:
        value = raw.get(name, getattr(settings, name, default))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        value = float(value)
        return value if math.isfinite(value) and low <= value <= high else default

    enabled = raw.get("crisis_guard_enabled", settings.crisis_guard_enabled) is True
    history = raw.get("crisis_guard_history_messages", settings.crisis_guard_history_messages)
    if isinstance(history, bool) or not isinstance(history, int) or not 1 <= history <= 50:
        history = 6
    return GuardConfig(
        enabled=enabled,
        timeout_s=bounded("crisis_guard_timeout_s", 2.0, 0.01, 2.0),
        threshold=bounded("crisis_guard_threshold", 0.4, 0.0, 1.0),
        history_messages=history,
    )


def ledger_tokens(raw: Mapping) -> dict[str, int] | None:
    total = raw.get("input_tokens")
    details = raw.get("input_tokens_details", {})
    if not isinstance(details, Mapping):
        return None
    read, write = details.get("cached_tokens", 0), details.get("cache_write_tokens", 0)
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0
           for v in (total, read, write)) or read + write > total:
        return None
    return {"input_tokens": total - read - write,
            "cached_input_tokens": read, "cache_write_tokens": write}


async def recorded_decision(
    text: str, recent: Sequence[crisis_guard.GuardMessage], *, config: GuardConfig,
    deadline: float, ledger: usage_ledger.LedgerContext,
) -> crisis_guard.DecisionResult:
    """판정 비용은 사용자 응답 토큰과 구분해 회사 원장에 남긴다."""
    check_deadline = min(deadline, time.monotonic() + config.timeout_s)
    # The opening request survives a fast classification; the cost finalizer waits
    # for its actual id rather than dropping known usage on an arbitrary DB timeout.
    opened = asyncio.create_task(usage_ledger.open_call(
        usage_ledger.with_purpose(ledger, "crisis_decision"),
        provider="openai", model=crisis_guard.MODEL,
    ))

    async def finalize(verdict=None):
        async with asyncio.timeout(10.0):
            call_id = await opened
            if verdict is None:
                await usage_ledger.close_unknown(call_id, error_code="crisis_decision_interrupted")
                return
            usage = ledger_tokens(verdict.raw_usage) if verdict.raw_usage is not None else None
            if usage is None:
                await usage_ledger.close_unknown(call_id, error_code=f"crisis_decision_{verdict.status}")
            else:
                await usage_ledger.close_call(
                    call_id, provider="openai", model=verdict.model,
                    **usage, output_tokens=0, provider_request_id=verdict.request_id,
                    latency_ms=round(verdict.latency_ms), preserve_on_cancel=True,
                )

    try:
        decision = await crisis_guard.decide(
            text, recent, api_key=settings.openai_api_key, deadline=check_deadline,
            timeout_s=config.timeout_s, threshold=config.threshold,
            history_limit=config.history_messages,
        )
    except BaseException:
        _finish_cost(finalize())
        raise
    _finish_cost(finalize(decision))
    _log.info(
        "crisis_guard route=%s status=%s followup=%s latency_ms=%d",
        decision.route, decision.status, decision.followup, round(decision.latency_ms),
    )
    return decision



Draft = tuple[llm.LLMResult | None, AgentTurn | None]


@dataclass(frozen=True)
class GuardedReply:
    result: llm.LLMResult | None
    agent_turn: AgentTurn | None
    decision: crisis_guard.DecisionResult
    discarded_result: llm.LLMResult | None = None
    discarded_agent: AgentTurn | None = None


async def generate(
    *, decision: Awaitable[crisis_guard.DecisionResult],
    draft: Callable[[Callable[[], Awaitable[bool]]], Awaitable[Draft]],
    reask: Callable[[], Awaitable[llm.LLMResult]],
    language: str, nickname: str | None, deadline: float,
    normal_error: Exception | None = None,
) -> GuardedReply:
    """도구는 판정 뒤에만, 초안은 동시에. 취소·실패 때 고아 태스크를 남기지 않는다."""
    decision_task = asyncio.ensure_future(decision)

    async def before_tools() -> bool:
        verdict = await asyncio.shield(decision_task)
        if verdict.route == "normal" and normal_error is not None:
            raise normal_error
        return verdict.route == "normal"

    draft_task = asyncio.create_task(draft(before_tools))
    try:
        verdict = await asyncio.shield(decision_task)
        if verdict.route == "normal":
            if normal_error is not None:
                raise normal_error
            result, agent = await draft_task
            return GuardedReply(result, agent, verdict)

        discarded_result = None
        discarded_agent = None
        if draft_task.done() and not draft_task.cancelled():
            try:
                discarded_result, discarded_agent = draft_task.result()
            except Exception:
                # A failed speculative model call cannot prevent a fixed safety reply.
                pass
        else:
            draft_task.cancel()
            await asyncio.gather(draft_task, return_exceptions=True)

        if verdict.route == "safety":
            text = crisis_guard.fixed_reply(language, nickname)
            result = llm.LLMResult(text, 0, 0, model=settings.model_chat, finish_reason="stop")
        else:
            result = None
            try:
                remaining = deadline - time.monotonic()
                if remaining > 0.05:
                    async with asyncio.timeout(remaining):
                        result = await reask()
                        llm.require_completed_text(result.text, result.finish_reason)
            except Exception:
                # Incomplete text must not survive as a valid generated recheck.
                result = None
            if result is None:
                result = llm.LLMResult("", 0, 0, model=settings.model_chat, finish_reason="stop")
            result.text = crisis_guard.checked_reask(
                result.text, language, method_request=verdict.method_request,
            )
        return GuardedReply(result, None, verdict, discarded_result, discarded_agent)
    finally:
        for task in (decision_task, draft_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(decision_task, draft_task, return_exceptions=True)
