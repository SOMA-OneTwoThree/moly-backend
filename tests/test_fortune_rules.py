from __future__ import annotations

import json
from collections import Counter
from datetime import date, timedelta
from inspect import signature
from types import MappingProxyType

import pytest

from app.services import fortune_ephemeris, fortune_rules

_BIRTH = {planet: 0.0 for planet in ("sun", "mercury", "venus", "mars", "jupiter", "saturn")}
_CURRENT = {
    planet: 30.0
    for planet in ("sun", "moon", "mercury", "venus", "mars", "jupiter", "saturn")
}


def _signal(key: str, current: str, x_bp: int, flow: str = "advance", priority: int = 1):
    return fortune_rules.Signal(
        key=key,
        current=current,
        birth="sun",
        aspect="trine" if x_bp > 0 else "square",
        aspect_priority=1,
        pair_priority=priority,
        flow=flow,
        q_bp=10_000 if x_bp > 0 else -10_000,
        k_bp=abs(x_bp),
        x_bp=x_bp,
    )


def _result(*, birth=None, current=None):
    return fortune_rules.generate_semantic_result(
        birth_positions=birth or _BIRTH,
        current_positions=current or _CURRENT,
        allow_unapproved=True,
    )


def test_unapproved_seed_rules_fail_closed_by_default():
    rules = fortune_rules.load_rule_assets()
    assert rules["approved_for_production"] is False
    with pytest.raises(fortune_rules.UnapprovedRulesError):
        fortune_rules.generate_semantic_result(
            birth_positions=_BIRTH,
            current_positions=_CURRENT,
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(-1.0, 0), (0.0, 0), (0.00005, 1), (0.5, 5000), (0.99995, 10_000), (2.0, 10_000)],
)
def test_strength_quantization_is_half_up_basis_points(raw, expected):
    assert fortune_rules.quantize_unit(raw) == expected


def test_score_is_symmetric_uses_zero_padding_and_reaches_extremes():
    assert fortune_rules.score_from_signed_sum(0) == 50
    assert fortune_rules.score_from_signed_sum(10_000) == 67
    assert fortune_rules.score_from_signed_sum(-10_000) == 33
    assert fortune_rules.score_from_signed_sum(30_000) == 100
    assert fortune_rules.score_from_signed_sum(-30_000) == 0
    for value in range(30_001):
        assert fortune_rules.score_from_signed_sum(value) + fortune_rules.score_from_signed_sum(-value) == 100


def test_all_decile_boundaries_are_fixed():
    scores = (0, 9, 10, 19, 20, 39, 40, 59, 60, 79, 80, 89, 90, 100)
    assert [fortune_rules.decile_for(score) for score in scores] == [
        "d00", "d00", "d10", "d10", "d20", "d30", "d40",
        "d50", "d60", "d70", "d80", "d80", "d90", "d90",
    ]


def test_select_top_three_deduplicates_current_and_ignores_fourth_signal():
    signals = [
        _signal("sun.strong", "sun", 9000, priority=1),
        _signal("sun.duplicate", "sun", 8000, priority=2),
        _signal("moon", "moon", 7000, priority=3),
        _signal("mars", "mars", 6000, priority=4),
        _signal("venus.fourth", "venus", -5000, priority=5),
    ]
    selected = fortune_rules.select_top_three(list(reversed(signals)))
    assert [signal.key for signal in selected] == ["sun.strong", "moon", "mars"]
    assert fortune_rules.score_signals(signals).signed_sum_bp == 22_000


def test_exact_trines_and_squares_reach_100_and_0():
    supportive = _result(current={key: 120.0 for key in _CURRENT})
    tension = _result(current={key: 90.0 for key in _CURRENT})
    assert supportive["overall"]["score"] == 100
    assert tension["overall"]["score"] == 0
    assert {item["score"] for item in supportive["categories"].values()} == {100}
    assert {item["score"] for item in tension["categories"].values()} == {0}


def test_no_signal_is_neutral_balance_clear_with_safe_routes():
    result = _result()
    assert result["overall"] == {
        "score": 50,
        "reading_code": "overall.d50.balance.clear",
        "expression_route": "overall.d50.balance.default",
    }
    assert set(result["categories"]) == {"love", "money", "work", "energy"}
    assert all(item["score"] == 50 for item in result["categories"].values())
    assert all(item["expression_route"].endswith(".d50.general") for item in result["categories"].values())


def test_semantic_result_is_input_order_invariant_and_has_no_uuid_variant():
    birth = dict(reversed(list(_BIRTH.items())))
    current = dict(reversed(list({key: 90.0 for key in _CURRENT}.items())))
    first = _result(birth=birth, current=current)
    second = _result(current={key: 90.0 for key in _CURRENT})
    assert json.dumps(first, sort_keys=True, separators=(",", ":")) == json.dumps(
        second, sort_keys=True, separators=(",", ":")
    )
    assert "user_id" not in signature(fortune_rules.generate_semantic_result).parameters


def test_birth_moon_is_not_required_or_used():
    with_extra_moon = dict(_BIRTH, moon=231.0)
    assert _result(birth=with_extra_moon) == _result(birth=_BIRTH)


def test_rule_assets_cover_all_pairs_categories_flows_and_colors():
    rules = fortune_rules.load_rule_assets()
    assert tuple(rules["current_planets"]) == tuple(_CURRENT)
    assert tuple(rules["birth_planets"]) == tuple(_BIRTH)
    assert len(rules["flow_matrix"]) == 7
    assert {slot["topic"] for slot in rules["category_slots"]["love"]} == {
        "conversation", "intimacy", "expression", "expectation", "distance"
    }
    assert len({color for row in rules["lucky_color_by_flow"].values() for color in row}) == 12


def test_rule_assets_are_deeply_immutable_and_fixed_constants_are_validated():
    rules = fortune_rules.load_rule_assets()
    assert isinstance(rules["aspects"], MappingProxyType)
    with pytest.raises(TypeError):
        rules["aspects"]["trine"]["q_bp"] = 123  # type: ignore[index]

    raw = json.loads(fortune_rules._RULES_PATH.read_text(encoding="utf-8"))
    raw["orbs"]["moon"] = True
    with pytest.raises(fortune_rules.FortuneRuleError, match="orbs"):
        fortune_rules._validate_rules(raw)
    raw = json.loads(fortune_rules._RULES_PATH.read_text(encoding="utf-8"))
    raw["aspects"]["trine"]["q_bp"] = -1234
    with pytest.raises(fortune_rules.FortuneRuleError, match="q_bp"):
        fortune_rules._validate_rules(raw)


def test_category_ties_use_declared_slot_priority():
    rules = fortune_rules.load_rule_assets()
    slots = rules["category_slots"]["money"]
    signals = fortune_rules._category_signals(
        birth_positions=_BIRTH,
        current_positions={key: 120.0 for key in _CURRENT},
        slots=slots,
        rules=rules,
    )
    assert [signal.pair_priority for signal in signals] == [10, 20, 30, 40, 50]
    result = fortune_rules._category_result(
        category="money",
        signals=signals,
        slots=slots,
    )
    assert result["score"] == 100
    assert result["reading_code"] == "category.money.d90.spending.clear"


def test_context_and_balance_threshold_boundaries_are_explicit():
    assert fortune_rules._context(
        (_signal("positive", "sun", 6000), _signal("negative", "moon", -2000)),
        4000,
    ) == "mixed"
    assert fortune_rules._context(
        (_signal("positive", "sun", 6001), _signal("negative", "moon", -1999)),
        4002,
    ) == "clear"

    rules = fortune_rules.load_rule_assets()
    below = fortune_rules.ScoreResult(
        score=70,
        signed_sum_bp=10_001,
        signals=(
            _signal("a", "sun", 4000, flow="start"),
            _signal("b", "moon", 3001, flow="advance"),
            _signal("c", "mars", 3000, flow="focus"),
        ),
    )
    boundary = fortune_rules.ScoreResult(
        score=67,
        signed_sum_bp=10_000,
        signals=(
            _signal("a", "sun", 4000, flow="start"),
            _signal("b", "moon", 3000, flow="advance"),
            _signal("c", "mars", 3000, flow="focus"),
        ),
    )
    assert fortune_rules._overall_flow(below, rules["flow_priority"]) == "balance"
    assert fortune_rules._overall_flow(boundary, rules["flow_priority"]) == "start"


def test_orb_boundary_is_exclusive_and_zero_direction_conjunction_is_ignored():
    rules = fortune_rules.load_rule_assets()
    inside = fortune_rules._match_signal(
        current_key="sun",
        birth_key="sun",
        current_longitude=119.0,
        birth_longitude=0.0,
        pair_priority=1,
        rules=rules,
    )
    assert inside is not None and inside.aspect == "trine" and inside.k_bp == 5000
    boundary = fortune_rules._match_signal(
        current_key="sun",
        birth_key="sun",
        current_longitude=118.0,
        birth_longitude=0.0,
        pair_priority=1,
        rules=rules,
    )
    assert boundary is None
    neutral = fortune_rules._match_signal(
        current_key="saturn",
        birth_key="sun",
        current_longitude=0.0,
        birth_longitude=0.0,
        pair_priority=1,
        rules=rules,
    )
    assert neutral is None


def test_fixed_distribution_covers_range_without_neutral_or_balance_collapse():
    birthdays = [
        date(year, month, day)
        for year in range(1960, 2011, 5)
        for month, day in ((1, 3), (4, 17), (7, 29), (10, 11))
    ]
    days = [date(2026, 1, 1) + timedelta(days=index * 7) for index in range(52)]
    birth_values = {
        value: fortune_ephemeris.date_chart_longitudes(
            value, "UTC", fortune_ephemeris.BIRTH_PLANET_KEYS
        )
        for value in birthdays
    }
    day_values = {
        value: fortune_ephemeris.date_chart_longitudes(
            value, "Asia/Seoul", fortune_ephemeris.PLANET_KEYS
        )
        for value in days
    }
    scores: list[int] = []
    flows: Counter[str] = Counter()
    category_neutral: Counter[str] = Counter()
    for birth_date in birthdays:
        for local_date in days:
            result = fortune_rules.generate_semantic_result(
                birth_positions=birth_values[birth_date],
                current_positions=day_values[local_date],
                allow_unapproved=True,
            )
            scores.append(result["overall"]["score"])
            flows[result["overall"]["reading_code"].split(".")[2]] += 1
            for category, item in result["categories"].items():
                if item["score"] == 50:
                    category_neutral[category] += 1

    sample_count = len(scores)
    assert min(scores) <= 5 and max(scores) >= 95 and len(set(scores)) >= 90
    assert {fortune_rules.decile_for(score) for score in scores} == {
        f"d{value:02d}" for value in range(0, 100, 10)
    }
    assert 0.20 <= flows["balance"] / sample_count <= 0.45
    assert all(category_neutral[key] / sample_count < 0.50 for key in category_neutral)
