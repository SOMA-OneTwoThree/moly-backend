"""Independent-score reproducibility and held-out distribution contract."""
from datetime import date, timedelta
from itertools import combinations
from statistics import correlation, mean, pstdev
from types import SimpleNamespace

import pytest

from app.services import fortune_scores


def test_known_result_and_domain_separation():
    b, d = date(2002, 12, 13), date(2026, 9, 9)
    assert [fortune_scores.score_for(b, d, axis) for axis in fortune_scores.AXES] == [42, 69, 82, 48, 60]
    assert fortune_scores.generate_result(birth_date=b, local_date=d)["schema_version"] == 4
    for axis in fortune_scores.AXES:
        before = fortune_scores.score_for(b, d, axis)
        for other in reversed(fortune_scores.AXES):
            fortune_scores.score_for(b, d + timedelta(days=1), other)
        assert fortune_scores.score_for(b, d, axis) == before


@pytest.mark.parametrize("birth,day,axis", [
    (date(1899, 12, 31), date(2026, 1, 1), "love"),
    (date(2027, 1, 1), date(2026, 1, 1), "money"),
    (date(2000, 1, 1), date(2026, 1, 1), "unknown"),
])
def test_invalid_input_is_not_a_neutral_score(birth, day, axis):
    with pytest.raises(ValueError):
        fortune_scores.score_for(birth, day, axis)


def test_held_out_axes_do_not_follow_overall_or_collapse_to_fifty():
    births = [date(1940, 2, 29) + timedelta(days=i * 139) for i in range(200)]
    days = [date(2028, 1, 1) + timedelta(days=i) for i in range(366)]
    rows = [[fortune_scores.score_for(b, d, a) for a in fortune_scores.AXES] for b in births for d in days]
    columns = list(zip(*rows))
    for col in columns:
        assert col.count(50) / len(col) <= .03
        assert pstdev(col) >= 16
        assert min(col) == 30 and max(col) == 100
        assert .13 < sum(30 <= score < 40 for score in col) / len(col) < .15
    for a, b in combinations(columns, 2):
        assert abs(correlation(a, b)) < .08
    for i in range(1, 5):
        low = [r[i] for r in rows if r[0] < 40]
        high = [r[i] for r in rows if r[0] >= 80]
        assert abs(mean(low) - mean(high)) < 3
    spreads = [max(r[1:]) - min(r[1:]) for r in rows]
    assert sum(s <= 10 for s in spreads) / len(spreads) <= .10
    assert sum(s >= 25 for s in spreads) / len(spreads) >= .65


@pytest.mark.parametrize("raw_score", range(101))
def test_floor_covers_every_original_score_on_every_axis(monkeypatch, raw_score):
    # Choose a digest in the middle of each original score's inverse-CDF interval.
    # Lock the original distribution independently of production constants.
    weights = (1, 2, 4, 7, 12, 18, 22, 18, 11, 5)
    band = min(raw_score // 10, 9)
    width = 11 if band == 9 else 10
    offset = raw_score - band * 10
    value = ((sum(weights[:band]) * 2 * width + weights[band] * (2 * offset + 1))
             * (1 << 256) // (100 * 2 * width))
    monkeypatch.setattr(fortune_scores, "sha256", lambda _: SimpleNamespace(
        digest=lambda: value.to_bytes(32, "big"),
    ))
    result = fortune_scores.generate_result(birth_date=date(2000, 1, 1), local_date=date(2026, 9, 10))
    expected = 30 + raw_score // 3 if raw_score < 30 else raw_score
    decile = min(expected // 10, 9) * 10
    for item in [result["overall"], *result["categories"].values()]:
        assert item["score"] == expected
        assert f".d{decile:02d}." in item["expression_route"]
        assert f".d{decile:02d}." in item["reading_code"]
    if raw_score < 30:
        assert result["lucky_color_key"] == "blue"
