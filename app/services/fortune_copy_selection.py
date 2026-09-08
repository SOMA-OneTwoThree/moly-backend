"""Bounded, per-route presentation cursors; fortune scores never depend on these."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from functools import lru_cache
from hashlib import sha256
from typing import Any, Mapping

SELECTION_VERSION = "fortune-selection.v2"
_LEGACY_VERSION = "fortune-selection.v1"
VARIANT_IDS = tuple(f"v{number:02d}" for number in range(1, 21))
CATEGORY_KEYS = ("love", "money", "work", "energy")
FLOW_KEYS = ("start", "advance", "focus", "coordinate", "change", "organize", "recover", "balance")
DECILES = tuple(f"d{number:02d}" for number in range(0, 100, 10))
SEMANTIC_OVERALL_ROUTES = frozenset(
    f"overall.{decile}.{flow}.default" for decile in DECILES for flow in FLOW_KEYS
)
OVERALL_ROUTES = frozenset(f"overall.{decile}.general" for decile in DECILES)
CATEGORY_ROUTES = frozenset(
    f"category.{category}.{decile}.general" for category in CATEGORY_KEYS for decile in DECILES
)
ALL_ROUTES = OVERALL_ROUTES | CATEGORY_ROUTES
_LEGACY_ROUTES = SEMANTIC_OVERALL_ROUTES | CATEGORY_ROUTES
SELECTED_KEYS = frozenset(("overall", *CATEGORY_KEYS))


class FortuneSelectionError(ValueError):
    """Do not silently reset a malformed or unknown presentation cursor."""


def _order(route: str, version: str) -> tuple[str, ...]:
    return tuple(sorted(
        VARIANT_IDS,
        key=lambda variant: (
            sha256(f"{version}|{route}|{variant}".encode("ascii")).digest(), variant,
        ),
    ))


@lru_cache(maxsize=50)
def variant_order(route: str) -> tuple[str, ...]:
    if route not in ALL_ROUTES:
        raise FortuneSelectionError("unknown fortune expression route")
    # Category copy is unchanged: retain its original permutation and cursor.
    version = _LEGACY_VERSION if route in CATEGORY_ROUTES else SELECTION_VERSION
    return _order(route, version)


def overall_copy_route(semantic_route: str) -> str:
    """Collapse calculation-only flow types into one editorial pool per score band."""
    if not isinstance(semantic_route, str) or semantic_route not in SEMANTIC_OVERALL_ROUTES:
        raise FortuneSelectionError("invalid semantic overall route")
    return f"overall.{semantic_route.split('.')[1]}.general"


def validate_selected(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != SELECTED_KEYS:
        raise FortuneSelectionError("five selected fortune variants are required")
    if any(not isinstance(v, str) or v not in VARIANT_IDS for v in value.values()):
        raise FortuneSelectionError("unknown selected fortune variant")
    return dict(value)


def _read_state(value: Any, *, legacy: bool) -> dict[str, dict[str, Any]]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"version", "selected", "routes"}
        or value["version"] != (_LEGACY_VERSION if legacy else SELECTION_VERSION)
    ):
        raise FortuneSelectionError("unsupported fortune selection state")
    validate_selected(value["selected"])
    routes = value["routes"]
    if not isinstance(routes, Mapping) or not set(routes).issubset(_LEGACY_ROUTES if legacy else ALL_ROUTES):
        raise FortuneSelectionError("invalid fortune cursor routes")
    for cursor in routes.values():
        if not isinstance(cursor, Mapping) or set(cursor) != {"day", "position"}:
            raise FortuneSelectionError("invalid fortune cursor")
        day, position = cursor["day"], cursor["position"]
        if not isinstance(day, str):
            raise FortuneSelectionError("invalid fortune cursor date")
        try:
            parsed = date.fromisoformat(day)
        except ValueError as exc:
            raise FortuneSelectionError("invalid fortune cursor date") from exc
        if parsed.isoformat() != day:
            raise FortuneSelectionError("noncanonical fortune cursor date")
        if isinstance(position, bool) or not isinstance(position, int) or not 0 <= position < 20:
            raise FortuneSelectionError("invalid fortune cursor position")
    return deepcopy(dict(routes))


def _result_routes(semantic: Mapping[str, Any], *, legacy: bool = False) -> dict[str, str]:
    try:
        routes = {"overall": semantic["overall"]["expression_route"]}
        routes.update({
            category: semantic["categories"][category]["expression_route"]
            for category in CATEGORY_KEYS
        })
    except (KeyError, TypeError) as exc:
        raise FortuneSelectionError("missing fortune result routes") from exc
    if any(not isinstance(route, str) for route in routes.values()):
        raise FortuneSelectionError("invalid fortune result routes")
    if routes["overall"] not in SEMANTIC_OVERALL_ROUTES or any(
        routes[category] not in CATEGORY_ROUTES
        or not routes[category].startswith(f"category.{category}.")
        for category in CATEGORY_KEYS
    ):
        raise FortuneSelectionError("invalid fortune result routes")
    if not legacy:
        routes["overall"] = overall_copy_route(routes["overall"])
    return routes


def select_variants(
    semantic: Mapping[str, Any],
    *,
    today: date,
    previous_semantic: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Advance only on a later local date, atomically with the five-copy snapshot.

    A cursor counts result construction, including locked details, not UI reads.
    Keeping at most 50 route cursors survives intervening routes without storing
    an unbounded event history. Date rollback and same-day profile edits reuse
    the cursor, rather than allowing refreshes to consume the remaining copies.
    """
    routes = {}
    if previous_semantic is not None and "copy_selection" in previous_semantic:
        state = previous_semantic["copy_selection"]
        legacy = isinstance(state, Mapping) and state.get("version") == _LEGACY_VERSION
        routes = _read_state(state, legacy=legacy)
        for key, route in _result_routes(previous_semantic, legacy=legacy).items():
            order = _order(route, _LEGACY_VERSION) if legacy else variant_order(route)
            if route not in routes or state["selected"][key] != order[routes[route]["position"]]:
                raise FortuneSelectionError("selected fortune variant does not match its cursor")
        if legacy:
            # Old overall IDs describe discarded, flow-specific copy. They have no
            # meaningful correspondence to the new general-day readings. Reset only
            # those pools, after validating the complete old state; keep categories.
            routes = {route: cursor for route, cursor in routes.items() if route in CATEGORY_ROUTES}
    chosen_routes = _result_routes(semantic)
    selected = {}
    for key, route in chosen_routes.items():
        order = variant_order(route)
        previous = routes.get(route)
        position = 0
        day = today.isoformat()
        if previous is not None:
            position = previous["position"]
            if today > date.fromisoformat(previous["day"]):
                position = (position + 1) % len(VARIANT_IDS)
            else:
                day = previous["day"]
        routes[route] = {"day": day, "position": position}
        selected[key] = order[position]
    return {"version": SELECTION_VERSION, "selected": selected, "routes": routes}
