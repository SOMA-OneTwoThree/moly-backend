"""Temporary metadata on an existing failure warning; no new events or persistence.

Expires at 2026-10-25 00:00 UTC. At most 100 enriched warnings per process
(restarts reset the count, never the absolute expiry). Remove after diagnosis.
Existing journald retention still governs already emitted warnings.
"""
import json
import time

from app.services.llm import IncompleteResponseError

_EXPIRES_AT = 1792886400  # 2026-10-25T00:00:00Z
_remaining = 100
_ENUMS = {
    "reason": {"unfinished", "pending_tools", "rejected_final", "blank_text"},
    "finish": {"stop", "tool_calls", "length", "content_filter", "", "other"},
    "stage": {"first", "final"},
}
_COUNTS = (
    "output_tokens", "max_tokens", "text_chars", "final_text_chars",
    "raw_tool_calls", "pending_tool_calls", "rejected_final_responses",
)


def suffix(exc: BaseException) -> str:
    global _remaining
    if not isinstance(exc, IncompleteResponseError) or not isinstance(exc.diagnostic, dict) or not exc.diagnostic:
        return ""
    if _remaining <= 0 or time.time() >= _EXPIRES_AT:
        return ""
    # Defense in depth: never serialize arbitrary exception/provider metadata.
    source = exc.diagnostic
    safe = {
        key: source[key] if isinstance(source.get(key), str) and source[key] in allowed else "other"
        for key, allowed in _ENUMS.items()
    }
    for key in _COUNTS:
        value = source.get(key)
        safe[key] = min(1_000_000, max(0, value)) if type(value) is int else None
    _remaining -= 1
    return " incomplete_diag=" + json.dumps(safe, separators=(",", ":"))
