"""Capture global projection controls in the short foreground acceptance transaction."""
from __future__ import annotations

import uuid

from sqlalchemy import text

from app.services.retention_preparation_context import Binding, set_binding

ACTIVE_KEY = "context_retention_active_preparation"
_CONTROL_KEYS = (
    ACTIVE_KEY, "context_retention_v2_enabled", "crisis_guard_enabled",
    "crisis_guard_policy_version", "crisis_guard_timeout_s", "crisis_guard_history_messages",
)
_CONTROLS = text("""SELECT key,value FROM app_config WHERE key=ANY(CAST(:keys AS text[]))
  ORDER BY key FOR SHARE""")


async def capture_controls(session, config):
    # Old-schema/NULL-v5 compatibility has no release row yet. Online migration
    # seeds JSON null, so all prepared production requests use the fenced branch.
    if ACTIVE_KEY not in config:
        set_binding(None)
        return config, None
    rows = (await session.execute(_CONTROLS, {"keys": list(_CONTROL_KEYS)})).all()
    fresh = dict(rows)
    if ACTIVE_KEY not in fresh:
        raise ValueError("retention_release_control_missing")
    captured = dict(config)
    for key in _CONTROL_KEYS:
        captured.pop(key, None)
    captured.update(fresh)
    identity = fresh[ACTIVE_KEY]
    if identity is None:
        value = None
    else:
        if not isinstance(identity, str):
            raise ValueError("retention_release_control_invalid")
        value = Binding(uuid.UUID(identity), False)
        from app.services.retention_release import require_binding
        await require_binding(session, value, lock=True)
    set_binding(value)
    return captured, value
