"""Experience score contracts; run in CI or the approved development harness."""
from datetime import date, datetime
import json

import pytest

from app.services import fortune_scores


def test_immutable_chart_evidence_and_repeatable_axis_order():
    birth, day = date(2002, 12, 13), date(2026, 9, 12)
    result = fortune_scores.generate_result(birth_date=birth, local_date=day)
    expected = [result["overall"]["score"], *[result["categories"][a]["score"] for a in fortune_scores.AXES[1:]]]
    assert [fortune_scores.score_for(birth, day, a) for a in fortune_scores.AXES] == expected
    assert result["astrology"]["reference_timezone"] == "UTC"
    assert result["astrology"]["ephemeris_version"] == fortune_scores.EPHEMERIS_VERSION
    result["astrology"]["semantic"]["overall"]["score"] = -100
    assert fortune_scores.generate_result(birth_date=birth, local_date=day)["astrology"]["semantic"]["overall"]["score"] >= 0
    for axis in reversed(fortune_scores.AXES):
        assert fortune_scores.score_for(birth, day, axis) == expected[fortune_scores.AXES.index(axis)]


@pytest.mark.parametrize("birth,day,axis", [
    (date(1899, 12, 31), date(2026, 1, 1), "love"),
    (date(2027, 1, 1), date(2026, 1, 1), "money"),
    (date(2000, 1, 1), date(2026, 1, 1), "unknown"),
    (datetime(2000, 1, 1), date(2026, 1, 1), "overall"),
])
def test_invalid_input_is_not_a_neutral_score(birth, day, axis):
    with pytest.raises(ValueError):
        fortune_scores.score_for(birth, day, axis)


def test_exact_distribution_contract():
    weights = fortune_scores.REGULAR_WEIGHTS
    normal_mean = sum(s * w for s, w in zip(range(70, 101), weights)) / sum(weights)
    assert abs(normal_mean * .998 + 64.5 * .002 - 88) < .15
    assert all(w > 0 for w in weights)
    assert len(weights) == 31
    assert fortune_scores.FIRST_WEIGHTS == tuple(reversed(fortune_scores.FIRST_WEIGHTS))


def test_rare_event_has_exactly_one_low_axis_and_never_applies_to_first(monkeypatch):
    original = fortune_scores._draw
    calls = []

    def controlled(seed, domain, size):
        calls.append(domain)
        if domain == "low-result":
            return 0
        if domain == "low-axis":
            return 2
        if domain.startswith("low-score"):
            return 0
        return original(seed, domain, size)

    monkeypatch.setattr(fortune_scores, "_draw", controlled)
    args = (date(2002, 12, 13), date(2026, 9, 12))
    normal = fortune_scores._scores(*args, False, "synthetic-astrology")
    assert normal["money"] == 60
    assert sum(s < 70 for s in normal.values()) == 1
    calls.clear()
    first = fortune_scores._scores(*args, True, "synthetic-astrology")
    assert all(90 <= s <= 100 for s in first.values())
    assert "low-result" not in calls


def test_astronomy_is_seed_input_and_color_source(monkeypatch):
    birth, day = date(2002, 12, 13), date(2026, 9, 12)
    evidence = json.loads(fortune_scores._astrology_json(birth, day))
    evidence["semantic"]["lucky_color_key"] = "coral"
    evidence["top_signals"] = [{"key": "test"}]
    monkeypatch.setattr(fortune_scores, "_astrology_json", lambda *_: json.dumps(evidence, sort_keys=True))
    seeds = []
    monkeypatch.setattr(fortune_scores, "_draw", lambda seed, domain, size: seeds.append(seed) or (1 % size))
    result = fortune_scores.generate_result(birth_date=birth, local_date=day)
    assert result["lucky_color_key"] == "orange"
    assert all('"key": "test"' in seed for seed in seeds)
