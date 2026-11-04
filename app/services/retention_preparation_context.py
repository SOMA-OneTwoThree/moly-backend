"""Request/job-local projection identity; never a user rollout selection.

Explicitly captured None keeps an old request on its original public reader while
another transaction activates a prepared release. Tokens are always restored.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator
import uuid


@dataclass(frozen=True, slots=True)
class Binding:
    preparation_id: uuid.UUID
    building: bool = False

    def __post_init__(self):
        if not isinstance(self.preparation_id, uuid.UUID) or type(self.building) is not bool:
            raise ValueError("invalid_preparation_binding")


_UNCAPTURED = object()
_binding: ContextVar[object] = ContextVar("retention_preparation", default=_UNCAPTURED)


def binding_captured() -> bool:
    return _binding.get() is not _UNCAPTURED


def current_binding() -> Binding | None:
    value = _binding.get()
    return value if isinstance(value, Binding) else None


@contextmanager
def use_binding(value: Binding | None) -> Iterator[None]:
    if value is not None and not isinstance(value, Binding):
        raise ValueError("invalid_preparation_binding")
    token = _binding.set(value)
    try:
        yield
    finally:
        _binding.reset(token)


@contextmanager
def without_binding() -> Iterator[None]:
    """A newly claimed job captures its own projection, not its producer task's."""
    token = _binding.set(_UNCAPTURED)
    try:
        yield
    finally:
        _binding.reset(token)


def set_binding(value: Binding | None) -> None:
    """Pin inside an already isolated request scope; its owner restores the token."""
    if value is not None and not isinstance(value, Binding):
        raise ValueError("invalid_preparation_binding")
    _binding.set(value)


def from_payload(payload: dict | None) -> Binding | None:
    body = payload or {}
    identity = body.get("retention_preparation_id")
    building = body.get("retention_preparation_building", False)
    if identity is None:
        if "retention_preparation_building" in body:
            raise ValueError("preparation_identity_required")
        return None
    if not isinstance(identity, str) or type(building) is not bool:
        raise ValueError("invalid_preparation_binding")
    try:
        value = uuid.UUID(identity)
    except (ValueError, AttributeError) as exc:
        raise ValueError("invalid_preparation_binding") from exc
    if str(value) != identity:
        raise ValueError("invalid_preparation_binding")
    return Binding(value, building)
