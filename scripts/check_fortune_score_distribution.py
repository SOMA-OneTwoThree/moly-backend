"""Experience distribution audit, gated by a verified existing development DB.

Only synthetic birth dates are scored; the remote database receives SELECT 1.
No local database, Docker, or production connection is used.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date, timedelta
from itertools import combinations
import json
from pathlib import Path
from statistics import correlation, mean, median, pstdev
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.fortune_scores import (  # noqa: E402
    AXES, FIRST_WEIGHTS, REGULAR_WEIGHTS, RULE_VERSION, generate_result,
)


def theory() -> dict:
    regular_mean = sum(s * w for s, w in zip(range(70, 101), REGULAR_WEIGHTS)) / sum(REGULAR_WEIGHTS)
    # Exactly one of five axes is replaced in 1% of regular results.
    return {
        "regular_non_low_mean": regular_mean,
        "regular_per_axis_mean": regular_mean * .998 + 64.5 * .002,
        "regular_low_result_pct": 1,
        "first_mean": sum(s * w for s, w in zip(range(90, 101), FIRST_WEIGHTS)) / sum(FIRST_WEIGHTS),
        "regular_weights": dict(zip(range(70, 101), REGULAR_WEIGHTS)),
    }


def audit(year: int, *, first_visit: bool = False) -> dict:
    birthdays = [date(1940, 2, 29) + timedelta(days=i * 139) for i in range(200)]
    start, end = date(year, 1, 1), date(year + 1, 1, 1)
    days = [start + timedelta(days=i) for i in range((end - start).days)]
    rows = []
    for birth in birthdays:
        for day in days:
            result = generate_result(birth_date=birth, local_date=day, first_visit=first_visit)
            rows.append([result["overall"]["score"], *[result["categories"][a]["score"] for a in AXES[1:]]])
    columns = list(zip(*rows))
    pairs = {f"{AXES[i]}/{AXES[j]}": round(correlation(columns[i], columns[j]), 5)
             for i, j in combinations(range(5), 2)}
    axes = {
        axis: {"min": min(col), "max": max(col), "mean": round(mean(col), 4),
               "median": median(col), "sd": round(pstdev(col), 4),
               "counts": {score: col.count(score) for score in range(60, 101)}}
        for axis, col in zip(AXES, columns)
    }
    low_counts = [sum(score < 70 for score in row) for row in rows]
    low_pct = 100 * sum(n > 0 for n in low_counts) / len(rows)
    passed = all(abs(v) < .04 for v in pairs.values())
    if first_visit:
        passed = passed and all(a["min"] == 90 and a["max"] == 100
                                and abs(a["mean"] - 95) < .1 for a in axes.values())
        passed = passed and low_pct == 0
    else:
        passed = passed and all(a["min"] == 60 and a["max"] == 100
                                and abs(a["mean"] - 88) < .3
                                and 87 <= a["median"] <= 89 for a in axes.values())
        passed = passed and .8 < low_pct < 1.2 and max(low_counts) <= 1
    return {"version": RULE_VERSION, "year": year, "first_visit": first_visit,
            "sample_count": len(rows), "passed": passed, "axes": axes,
            "pairs": pairs, "low_result_pct": round(low_pct, 4),
            "max_low_axes_per_result": max(low_counts)}


async def verify_development_target() -> None:
    import asyncpg
    from db.envfile import assert_dev_target, load_conn
    dsn = load_conn("dev")
    assert_dev_target("dev", dsn)
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=15)
    try:
        if await conn.fetchval("SELECT 1") != 1:
            raise RuntimeError("development database verification failed")
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", action="store_true", required=True,
                        help="Verify the existing allowed development Supabase before auditing")
    parser.add_argument("--years", nargs="+", type=int, default=[2026, 2028, 2031])
    args = parser.parse_args()
    asyncio.run(verify_development_target())
    results = [audit(y, first_visit=mode) for y in args.years for mode in (False, True)]
    print(json.dumps({"theory": theory(), "results": results}, indent=2))
    return int(not all(r["passed"] for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
