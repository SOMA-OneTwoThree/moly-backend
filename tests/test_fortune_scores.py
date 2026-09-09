"""Independent-score reproducibility and held-out distribution contract."""
from datetime import date, timedelta
from itertools import combinations
from statistics import correlation, mean, pstdev

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
        assert pstdev(col) >= 18
        assert min(col) == 0 and max(col) == 100
    for a, b in combinations(columns, 2):
        assert abs(correlation(a, b)) < .08
    for i in range(1, 5):
        low = [r[i] for r in rows if r[0] < 30]
        high = [r[i] for r in rows if r[0] >= 80]
        assert abs(mean(low) - mean(high)) < 3
    spreads = [max(r[1:]) - min(r[1:]) for r in rows]
    assert sum(s <= 10 for s in spreads) / len(spreads) <= .10
    assert sum(s >= 25 for s in spreads) / len(spreads) >= .65
