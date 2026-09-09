"""Deterministic release audit; synthetic birth dates only, no database access."""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from itertools import combinations
import json
from pathlib import Path
from statistics import correlation, mean, pstdev
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.fortune_scores import AXES, RULE_VERSION, score_for  # noqa: E402


def ranks(values):
    positions = {}
    for i, value in enumerate(sorted(values)):
        positions.setdefault(value, []).append(i)
    averages = {v: mean(indices) for v, indices in positions.items()}
    return [averages[v] for v in values]


def audit(year: int) -> dict:
    birthdays = [date(1940, 2, 29) + timedelta(days=i * 139) for i in range(200)]
    start, end = date(year, 1, 1), date(year + 1, 1, 1)
    days = [start + timedelta(days=i) for i in range((end - start).days)]
    rows = [[score_for(b, d, a) for a in AXES] for b in birthdays for d in days]
    columns = list(zip(*rows))
    ranked = [ranks(c) for c in columns]
    pairs = {}
    for i, j in combinations(range(5), 2):
        pairs[f'{AXES[i]}/{AXES[j]}'] = {
            'pearson': round(correlation(columns[i], columns[j]), 4),
            'spearman': round(correlation(ranked[i], ranked[j]), 4),
        }
    axes = {}
    for axis, col in zip(AXES, columns):
        axes[axis] = {
            'min': min(col), 'max': max(col), 'mean': round(mean(col), 2),
            'sd': round(pstdev(col), 2), 'exact50_pct': round(100 * col.count(50) / len(col), 2),
            'band_pct': [round(100 * sum(min(s // 10, 9) == b for s in col) / len(col), 2) for b in range(10)],
        }
    conditional = {
        AXES[i]: [round(mean(r[i] for r in rows if min(r[0] // 10, 9) == b), 2) for b in range(10)]
        for i in range(1, 5)
    }
    spreads = [max(r[1:]) - min(r[1:]) for r in rows]
    small = 100 * sum(s <= 10 for s in spreads) / len(spreads)
    large = 100 * sum(s >= 25 for s in spreads) / len(spreads)
    passed = (
        all(abs(v) < .08 for p in pairs.values() for v in p.values())
        and all(a['exact50_pct'] <= 3 and a['sd'] >= 18 for a in axes.values())
        and small <= 10 and large >= 65
        and all(max(c) - min(c) < 3 for c in conditional.values())
    )
    return {
        'version': RULE_VERSION, 'year': year, 'sample_count': len(rows), 'passed': passed,
        'axes': axes, 'pairs': pairs, 'conditional_category_means_by_overall_band': conditional,
        'category_spread_at_most_10_pct': round(small, 2),
        'category_spread_at_least_25_pct': round(large, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--years', nargs='+', type=int, default=[2026, 2028, 2031])
    args = parser.parse_args()
    results = [audit(y) for y in args.years]
    print(json.dumps(results, indent=2))
    return int(not all(r['passed'] for r in results))


if __name__ == '__main__':
    raise SystemExit(main())
