"""오늘의 운세 v2 결정적 점수·의미 코드 계산.

이 모듈은 문장을 만들지 않는다. 생년월일 하루 기준값과 오늘 하루 기준값에서 신호를 찾고,
중복 제거한 상위 세 신호로 점수와 문구 route만 결정한다.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Mapping, Sequence

from app.services.fortune_ephemeris import (
    BIRTH_PLANET_KEYS,
    PLANET_KEYS,
    angular_separation,
)

_RESOURCE_DIR: Final = Path(__file__).resolve().parents[1] / "resources" / "fortune"
_RULES_PATH: Final = _RESOURCE_DIR / "rules.v2.json"
_ASPECTS: Final = ("conjunction", "sextile", "square", "trine", "opposition")
_FLOWS: Final = (
    "start",
    "advance",
    "focus",
    "coordinate",
    "change",
    "organize",
    "recover",
    "balance",
)
_CATEGORIES: Final = ("love", "money", "work", "energy")
_COLOR_KEYS: Final = {
    "red", "coral", "orange", "yellow", "green", "sky",
    "blue", "navy", "purple", "pink", "white", "beige",
}
_CATEGORY_TOPICS: Final = {
    "love": {"conversation", "intimacy", "expression", "expectation", "distance"},
    "money": {"spending", "management", "planning", "opportunity", "risk"},
    "work": {"focus", "execution", "collaboration", "adjustment", "result"},
    "energy": {"activity", "balance", "recovery", "maintenance", "pacing"},
}
_BALANCE_THRESHOLD_PERCENT: Final = 40
RULE_VERSION: Final = "fortune-rules.v2.1"


class FortuneRuleError(ValueError):
    """규칙 자산이나 계산 입력이 v2 계약을 위반했다."""


class UnapprovedRulesError(FortuneRuleError):
    """검수 전 개발 규칙을 운영 경로에서 실행하려 했다."""


@dataclass(frozen=True, slots=True)
class Signal:
    key: str
    current: str
    birth: str
    aspect: str
    aspect_priority: int
    pair_priority: int
    flow: str
    q_bp: int
    k_bp: int
    x_bp: int


@dataclass(frozen=True, slots=True)
class ScoreResult:
    score: int
    signed_sum_bp: int
    signals: tuple[Signal, ...]


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FortuneRuleError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FortuneRuleError(f"cannot load rule asset: {path.name}") from exc
    if not isinstance(value, dict):
        raise FortuneRuleError("rule asset must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise FortuneRuleError(
            f"{label} keys mismatch: missing={sorted(expected-actual)}, "
            f"extra={sorted(actual-expected)}"
        )


def _validate_rules(rules: dict[str, Any]) -> None:
    _exact_keys(
        rules,
        {
            "schema_id", "rule_version", "semantic_result_schema_version",
            "approved_for_production", "approval_note", "birth_planets",
            "current_planets", "flow_priority", "orbs", "aspects",
            "category_orbs", "conjunction_q_bp", "flow_matrix", "category_slots",
            "lucky_color_by_flow",
        },
        "rules",
    )
    if rules["schema_id"] != "fortune-rules-schema.v2":
        raise FortuneRuleError("unexpected rules schema")
    if rules["rule_version"] != RULE_VERSION:
        raise FortuneRuleError("unexpected rule version")
    if rules["semantic_result_schema_version"] != 3:
        raise FortuneRuleError("unexpected semantic result schema")
    if not isinstance(rules["approved_for_production"], bool):
        raise FortuneRuleError("approved_for_production must be boolean")
    if tuple(rules["birth_planets"]) != BIRTH_PLANET_KEYS:
        raise FortuneRuleError("birth planet contract changed")
    if tuple(rules["current_planets"]) != PLANET_KEYS:
        raise FortuneRuleError("current planet contract changed")
    if tuple(rules["flow_priority"]) != _FLOWS:
        raise FortuneRuleError("flow priority contract changed")

    orbs = rules["orbs"]
    _exact_keys(orbs, {"moon", "default"}, "orbs")
    if (
        isinstance(orbs["moon"], bool)
        or isinstance(orbs["default"], bool)
        or not isinstance(orbs["moon"], (int, float))
        or not isinstance(orbs["default"], (int, float))
        or float(orbs["moon"]) != 5.0
        or float(orbs["default"]) != 2.0
    ):
        raise FortuneRuleError("v2 orbs must be moon=5 and default=2")
    category_orbs = rules["category_orbs"]
    _exact_keys(category_orbs, {"moon", "default"}, "category orbs")
    if (
        isinstance(category_orbs["moon"], bool)
        or isinstance(category_orbs["default"], bool)
        or not isinstance(category_orbs["moon"], (int, float))
        or not isinstance(category_orbs["default"], (int, float))
        or float(category_orbs["moon"]) != 7.0
        or float(category_orbs["default"]) != 4.0
    ):
        raise FortuneRuleError("v2 category orbs must be moon=7 and default=4")

    aspects = rules["aspects"]
    _exact_keys(aspects, set(_ASPECTS), "aspects")
    expected_angles = dict(zip(_ASPECTS, (0, 60, 90, 120, 180), strict=True))
    expected_q = {
        "conjunction": None,
        "sextile": 6667,
        "square": -10_000,
        "trine": 10_000,
        "opposition": -10_000,
    }
    for key, angle in expected_angles.items():
        item = aspects[key]
        _exact_keys(item, {"angle", "q_bp", "priority"}, f"aspect {key}")
        if item["angle"] != angle or item["priority"] != _ASPECTS.index(key) + 1:
            raise FortuneRuleError(f"invalid aspect {key}")
        if item["q_bp"] != expected_q[key]:
            raise FortuneRuleError(f"invalid q_bp for {key}")

    conjunction = rules["conjunction_q_bp"]
    flow_matrix = rules["flow_matrix"]
    _exact_keys(conjunction, set(PLANET_KEYS), "conjunction matrix")
    _exact_keys(flow_matrix, set(PLANET_KEYS), "flow matrix")
    for current in PLANET_KEYS:
        _exact_keys(conjunction[current], set(BIRTH_PLANET_KEYS), f"conjunction {current}")
        _exact_keys(flow_matrix[current], set(BIRTH_PLANET_KEYS), f"flow {current}")
        for birth in BIRTH_PLANET_KEYS:
            q_bp = conjunction[current][birth]
            if q_bp not in {-6667, 0, 6667}:
                raise FortuneRuleError(f"invalid conjunction q for {current}.{birth}")
            if flow_matrix[current][birth] not in _FLOWS:
                raise FortuneRuleError(f"invalid flow for {current}.{birth}")

    slots = rules["category_slots"]
    _exact_keys(slots, set(_CATEGORIES), "category slots")
    for category in _CATEGORIES:
        items = slots[category]
        if not isinstance(items, list) or len(items) != 5:
            raise FortuneRuleError(f"{category} must have five category slots")
        seen_topics: set[str] = set()
        seen_pairs: set[tuple[str, str]] = set()
        priorities: list[int] = []
        for item in items:
            _exact_keys(item, {"current", "birth", "topic", "priority"}, f"{category} slot")
            pair = (item["current"], item["birth"])
            if pair[0] not in PLANET_KEYS or pair[1] not in BIRTH_PLANET_KEYS:
                raise FortuneRuleError(f"invalid category pair: {category}.{pair}")
            if (
                item["topic"] not in _CATEGORY_TOPICS[category]
                or isinstance(item["priority"], bool)
                or not isinstance(item["priority"], int)
            ):
                raise FortuneRuleError(f"invalid category topic: {category}.{item['topic']}")
            priorities.append(item["priority"])
            seen_topics.add(item["topic"])
            if pair in seen_pairs:
                raise FortuneRuleError(f"duplicate category pair: {category}.{pair}")
            seen_pairs.add(pair)
        if seen_topics != _CATEGORY_TOPICS[category]:
            raise FortuneRuleError(f"category topics incomplete: {category}")
        if priorities != [10, 20, 30, 40, 50]:
            raise FortuneRuleError(f"category priorities changed: {category}")

    colors = rules["lucky_color_by_flow"]
    _exact_keys(colors, set(_FLOWS), "lucky colors")
    used_colors: set[str] = set()
    for flow in _FLOWS:
        row = colors[flow]
        if not isinstance(row, list) or len(row) != 10 or not set(row) <= _COLOR_KEYS:
            raise FortuneRuleError(f"invalid lucky color row: {flow}")
        used_colors.update(row)
    if used_colors != _COLOR_KEYS:
        raise FortuneRuleError("lucky color table must use all twelve colors")


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


@lru_cache(maxsize=1)
def load_rule_assets() -> Mapping[str, Any]:
    rules = _read_json(_RULES_PATH)
    _validate_rules(rules)
    return _deep_freeze(rules)


def quantize_unit(raw: float) -> int:
    """0...1 값을 0...10000 basis point로 half-up 양자화한다."""

    if not math.isfinite(raw):
        raise FortuneRuleError("strength must be finite")
    return math.floor(min(1.0, max(0.0, raw)) * 10_000 + 0.5)


def _round_signed(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise FortuneRuleError("denominator must be positive")
    sign = (numerator > 0) - (numerator < 0)
    return sign * ((abs(numerator) + denominator // 2) // denominator)


def score_from_signed_sum(signed_sum_bp: int) -> int:
    """상위 세 신호 합을 중립 50 기준 대칭 half-away 방식으로 0...100에 매핑한다."""

    if isinstance(signed_sum_bp, bool) or not isinstance(signed_sum_bp, int):
        raise FortuneRuleError("signed_sum_bp must be an integer")
    if not -30_000 <= signed_sum_bp <= 30_000:
        raise FortuneRuleError("signed_sum_bp must be in [-30000, 30000]")
    return min(100, max(0, 50 + _round_signed(signed_sum_bp * 50, 30_000)))


def decile_for(score: int) -> str:
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise FortuneRuleError("score must be an integer in [0, 100]")
    return f"d{min(score // 10, 9) * 10:02d}"


def _positions(value: Mapping[str, float], required: Sequence[str], label: str) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise FortuneRuleError(f"{label} positions must be a mapping")
    missing = set(required).difference(value)
    if missing:
        raise FortuneRuleError(f"{label} positions missing: {sorted(missing)}")
    result: dict[str, float] = {}
    for key in required:
        longitude = value[key]
        if (
            isinstance(longitude, bool)
            or not isinstance(longitude, (int, float))
            or not math.isfinite(float(longitude))
            or not 0 <= float(longitude) < 360
        ):
            raise FortuneRuleError(f"invalid {label} longitude: {key}")
        result[key] = float(longitude)
    return result


def _match_signal(
    *,
    current_key: str,
    birth_key: str,
    current_longitude: float,
    birth_longitude: float,
    pair_priority: int,
    rules: Mapping[str, Any],
    orb_table: str = "orbs",
) -> Signal | None:
    separation = angular_separation(current_longitude, birth_longitude)
    orb = float(rules[orb_table]["moon" if current_key == "moon" else "default"])
    matches: list[Signal] = []
    for aspect_key in _ASPECTS:
        aspect = rules["aspects"][aspect_key]
        distance = abs(separation - float(aspect["angle"]))
        if distance >= orb:
            continue
        k_bp = quantize_unit(1.0 - distance / orb)
        q_bp = (
            int(rules["conjunction_q_bp"][current_key][birth_key])
            if aspect_key == "conjunction"
            else int(aspect["q_bp"])
        )
        x_bp = _round_signed(q_bp * k_bp, 10_000)
        if x_bp == 0:
            continue
        matches.append(
            Signal(
                key=f"{current_key}.{birth_key}.{aspect_key}",
                current=current_key,
                birth=birth_key,
                aspect=aspect_key,
                aspect_priority=int(aspect["priority"]),
                pair_priority=pair_priority,
                flow=str(rules["flow_matrix"][current_key][birth_key]),
                q_bp=q_bp,
                k_bp=k_bp,
                x_bp=x_bp,
            )
        )
    if not matches:
        return None
    return min(matches, key=_signal_order)


def _signal_order(signal: Signal) -> tuple[int, int, int, str]:
    return (-abs(signal.x_bp), signal.pair_priority, signal.aspect_priority, signal.key)


def detect_signals(
    *, birth_positions: Mapping[str, float], current_positions: Mapping[str, float]
) -> tuple[Signal, ...]:
    """42개 current×birth 쌍의 유효 신호를 입력 순서와 무관하게 반환한다."""

    rules = load_rule_assets()
    birth = _positions(birth_positions, BIRTH_PLANET_KEYS, "birth")
    current = _positions(current_positions, PLANET_KEYS, "current")
    found: list[Signal] = []
    for current_index, current_key in enumerate(PLANET_KEYS):
        for birth_index, birth_key in enumerate(BIRTH_PLANET_KEYS):
            signal = _match_signal(
                current_key=current_key,
                birth_key=birth_key,
                current_longitude=current[current_key],
                birth_longitude=birth[birth_key],
                pair_priority=current_index * len(BIRTH_PLANET_KEYS) + birth_index,
                rules=rules,
            )
            if signal is not None:
                found.append(signal)
    return tuple(sorted(found, key=_signal_order))


def select_top_three(signals: Sequence[Signal]) -> tuple[Signal, ...]:
    """같은 current 요소를 한 번만 남긴 뒤 강한 신호 최대 세 개를 고른다."""

    best_by_current: dict[str, Signal] = {}
    for signal in signals:
        previous = best_by_current.get(signal.current)
        if previous is None or _signal_order(signal) < _signal_order(previous):
            best_by_current[signal.current] = signal
    return tuple(sorted(best_by_current.values(), key=_signal_order)[:3])


def score_signals(signals: Sequence[Signal]) -> ScoreResult:
    selected = select_top_three(signals)
    signed_sum = sum(signal.x_bp for signal in selected)
    return ScoreResult(
        score=score_from_signed_sum(signed_sum),
        signed_sum_bp=signed_sum,
        signals=selected,
    )


def _context(signals: Sequence[Signal], signed_sum: int) -> str:
    if not signals:
        return "clear"
    if signed_sum == 0:
        return "mixed"
    direction = 1 if signed_sum > 0 else -1
    total = sum(abs(signal.x_bp) for signal in signals)
    opposite = sum(abs(signal.x_bp) for signal in signals if signal.x_bp * direction < 0)
    return "mixed" if opposite * 4 >= total else "clear"


def _overall_flow(result: ScoreResult, flow_priority: Sequence[str]) -> str:
    if not result.signals or result.signed_sum_bp == 0:
        return "balance"
    direction = 1 if result.signed_sum_bp > 0 else -1
    aligned = [signal for signal in result.signals if signal.x_bp * direction > 0]
    priority = {flow: index for index, flow in enumerate(flow_priority)}
    representative = min(
        aligned,
        key=lambda signal: (-abs(signal.x_bp), priority[signal.flow], _signal_order(signal)),
    )
    total = sum(abs(signal.x_bp) for signal in result.signals)
    distinct_flows = {signal.flow for signal in result.signals}
    if (
        abs(representative.x_bp) * 100 < total * _BALANCE_THRESHOLD_PERCENT
        and len(distinct_flows) >= 2
    ):
        return "balance"
    return representative.flow


def _category_result(
    *,
    category: str,
    signals: Sequence[Signal],
    slots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    score_result = score_signals(signals)
    decile = decile_for(score_result.score)
    context = _context(score_result.signals, score_result.signed_sum_bp)
    topic = "general"
    if score_result.signed_sum_bp != 0:
        direction = 1 if score_result.signed_sum_bp > 0 else -1
        selected = [signal for signal in score_result.signals if signal.x_bp * direction > 0]
        representative = min(selected, key=_signal_order)
        topic_by_pair = {
            (str(slot["current"]), str(slot["birth"])): str(slot["topic"])
            for slot in slots
        }
        topic = topic_by_pair[(representative.current, representative.birth)]
    return {
        "score": score_result.score,
        "reading_code": f"category.{category}.{decile}.{topic}.{context}",
        "expression_route": f"category.{category}.{decile}.general",
    }


def _category_signals(
    *,
    birth_positions: Mapping[str, float],
    current_positions: Mapping[str, float],
    slots: Sequence[Mapping[str, Any]],
    rules: Mapping[str, Any],
) -> tuple[Signal, ...]:
    found: list[Signal] = []
    for slot in slots:
        current_key = str(slot["current"])
        birth_key = str(slot["birth"])
        signal = _match_signal(
            current_key=current_key,
            birth_key=birth_key,
            current_longitude=current_positions[current_key],
            birth_longitude=birth_positions[birth_key],
            pair_priority=int(slot["priority"]),
            rules=rules,
            orb_table="category_orbs",
        )
        if signal is not None:
            found.append(signal)
    return tuple(sorted(found, key=_signal_order))


def generate_semantic_result(
    *,
    birth_positions: Mapping[str, float],
    current_positions: Mapping[str, float],
    allow_unapproved: bool = False,
) -> dict[str, Any]:
    """점수, 의미 코드, 승인된 seed route만 담은 compact semantic result를 만든다."""

    rules = load_rule_assets()
    if not rules["approved_for_production"] and not allow_unapproved:
        raise UnapprovedRulesError(f"{RULE_VERSION} is not approved for production")
    signals = detect_signals(
        birth_positions=birth_positions,
        current_positions=current_positions,
    )
    overall = score_signals(signals)
    overall_decile = decile_for(overall.score)
    overall_flow = _overall_flow(overall, rules["flow_priority"])
    overall_context = _context(overall.signals, overall.signed_sum_bp)
    reading_code = f"overall.{overall_decile}.{overall_flow}.{overall_context}"

    categories = {
        category: _category_result(
            category=category,
            signals=_category_signals(
                birth_positions=birth_positions,
                current_positions=current_positions,
                slots=rules["category_slots"][category],
                rules=rules,
            ),
            slots=rules["category_slots"][category],
        )
        for category in _CATEGORIES
    }
    color_index = min(overall.score // 10, 9)
    return {
        "schema_version": 3,
        "overall": {
            "score": overall.score,
            "reading_code": reading_code,
            "expression_route": f"overall.{overall_decile}.{overall_flow}.default",
        },
        "categories": categories,
        "lucky_color_key": rules["lucky_color_by_flow"][overall_flow][color_index],
    }
