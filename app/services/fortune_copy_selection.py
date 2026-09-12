"""Bounded, per-route presentation cursors; fortune scores never depend on these."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from functools import lru_cache
from hashlib import sha256
from typing import Any, Mapping

SELECTION_VERSION = "fortune-selection.v3"
_PREVIOUS_VERSION = "fortune-selection.v2"
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
    return _order(route, SELECTION_VERSION)


def overall_copy_route(semantic_route: str) -> str:
    """Collapse calculation-only flow types into one editorial pool per score band."""
    if isinstance(semantic_route, str) and semantic_route in OVERALL_ROUTES:
        return semantic_route
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
        or value["version"] not in (
            (_LEGACY_VERSION,) if legacy else (_PREVIOUS_VERSION, SELECTION_VERSION)
        )
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
    allowed_overall = OVERALL_ROUTES if semantic.get("schema_version") == 4 else SEMANTIC_OVERALL_ROUTES
    if routes["overall"] not in allowed_overall or any(
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
        if isinstance(state, Mapping) and state.get("version") == CARD_SELECTION_VERSION:
            validate_card_selection(state)
            return select_variants(semantic, today=today)
        routes = _read_state(state, legacy=legacy)
        for key, route in _result_routes(previous_semantic, legacy=legacy).items():
            old_version = state["version"]
            order_version = (
                _LEGACY_VERSION if legacy or (old_version == _PREVIOUS_VERSION and route in CATEGORY_ROUTES)
                else old_version
            )
            order = _order(route, order_version)
            if route not in routes or state["selected"][key] != order[routes[route]["position"]]:
                raise FortuneSelectionError("selected fortune variant does not match its cursor")
        if state["version"] != SELECTION_VERSION:
            # Both overall and category prose changed. Validate the old chain
            # before resetting its editorial cursors; never rewrite its snapshot.
            routes = {}
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


CARD_SELECTION_VERSION = "fortune-selection.v4"


def validate_card_selection(value: Any) -> dict[str, Any]:
    """Read persisted draws strictly; unknown versions must never reset silently."""
    from app.services.fortune_tarot import AXES, CARD_IDS
    if not isinstance(value, Mapping) or set(value) != {"version", "day", "cards"}:
        raise FortuneSelectionError("invalid card selection fields")
    if value["version"] != CARD_SELECTION_VERSION:
        raise FortuneSelectionError("unsupported card selection version")
    try:
        day = date.fromisoformat(value["day"])
    except (ValueError, TypeError) as exc:
        raise FortuneSelectionError("invalid card selection date") from exc
    if day.isoformat() != value["day"]:
        raise FortuneSelectionError("noncanonical card selection date")
    cards = value["cards"]
    if not isinstance(cards, Mapping) or set(cards) != set(AXES):
        raise FortuneSelectionError("five card selections are required")
    seen = set()
    for card in cards.values():
        if not isinstance(card, Mapping) or set(card) != {"card_id", "orientation"}:
            raise FortuneSelectionError("invalid card selection")
        if not isinstance(card["card_id"], str) or card["card_id"] not in CARD_IDS:
            raise FortuneSelectionError("unknown selected card")
        if card["orientation"] not in ("upright", "reversed"):
            raise FortuneSelectionError("invalid selected orientation")
        if card["card_id"] in seen:
            raise FortuneSelectionError("duplicate selected card")
        seen.add(card["card_id"])
    return deepcopy(dict(value))


def validate_previous_selection(semantic: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if semantic is None or "copy_selection" not in semantic:
        return None
    state = semantic["copy_selection"]
    if isinstance(state, Mapping) and state.get("version") == CARD_SELECTION_VERSION:
        return validate_card_selection(state)
    # Existing reader verifies both the legacy cursor and its selected variant.
    select_variants(semantic, today=date.min, previous_semantic=semantic)
    return None


DRAW_ALGORITHM_VERSION = "fortune-birth-draw.v1"


def draw_cards(*, today: date, birth_date: date | None = None, first_visit: bool = False,
               user_id: str | None = None) -> dict[str, Any]:
    """Score-free, versioned SHA-256 stream and unbiased Fisher-Yates draw.

    Explicit byte order, deck order and rejection sampling make this independent
    of Python's random implementation. User/date/version are the only inputs.
    """
    from uuid import UUID
    from app.services.fortune_tarot import AXES, CARD_IDS
    if birth_date is None:
        # Historical v4 fixtures and rollback readers; the new service never uses UUID entropy.
        seed = f"{CARD_SELECTION_VERSION}|{UUID(str(user_id))}|{today.isoformat()}".encode("ascii")
    else:
        if type(birth_date) is not date or type(today) is not date or not date(1900, 1, 1) <= birth_date <= today:
            raise FortuneSelectionError("invalid birth/date draw input")
        if type(first_visit) is not bool:
            raise FortuneSelectionError("invalid draw mode")
        if first_visit:
            from app.services.fortune_catalog import first_visit_selection
            return first_visit_selection(birth_date=birth_date, today=today)
        seed = f"{DRAW_ALGORITHM_VERSION}|{birth_date.isoformat()}|{today.isoformat()}|regular".encode("ascii")
    counter = 0

    def below(bound: int) -> int:
        nonlocal counter
        limit = (1 << 256) - ((1 << 256) % bound)
        while True:
            value = int.from_bytes(sha256(seed + counter.to_bytes(8, "big")).digest(), "big")
            counter += 1
            if value < limit:
                return value % bound

    deck = sorted(CARD_IDS)
    for index in range(len(deck) - 1, 0, -1):
        other = below(index + 1)
        deck[index], deck[other] = deck[other], deck[index]
    return {
        "version": CARD_SELECTION_VERSION,
        "day": today.isoformat(),
        "cards": {
            axis: {"card_id": deck[index], "orientation": ("upright", "reversed")[below(2)]}
            for index, axis in enumerate(AXES)
        },
    }
