"""Reproducible entertainment scores, independently drawn for five axes.

These scores do not measure astronomical signals or predict real outcomes.
The same birth date and local calendar date produce the same scores everywhere.
"""
from __future__ import annotations

from datetime import date
from hashlib import sha256

RULE_VERSION = "fortune-independent.v1"
EPHEMERIS_VERSION = "not-used.independent-v1"
AXES = ("overall", "love", "money", "work", "energy")
# Score-band probabilities in percent. They sum to 100; 90..100 includes 100.
BAND_WEIGHTS = (1, 2, 4, 7, 12, 18, 22, 18, 11, 5)
COLORS = ("white", "beige", "sky", "blue", "green", "green", "yellow", "orange", "pink", "red")


def score_for(birth_date: date, local_date: date, axis: str) -> int:
    if type(birth_date) is not date or type(local_date) is not date:
        raise ValueError("fortune dates must be calendar dates")
    if not date(1900, 1, 1) <= birth_date <= local_date:
        raise ValueError("invalid fortune birth date")
    if axis not in AXES:
        raise ValueError("unknown fortune axis")
    seed = f"{RULE_VERSION}|{birth_date.isoformat()}|{local_date.isoformat()}|{axis}"
    # Integer inverse CDF: no process-random hash, floating-point boundary,
    # shared day modifier, or post-selection adjustment between axes.
    value = int.from_bytes(sha256(seed.encode("ascii")).digest(), "big")
    scale = 1 << 256
    bucket = value * 100
    lower = 0
    for band, weight in enumerate(BAND_WEIGHTS):
        if bucket < (lower + weight) * scale:
            width = 11 if band == 9 else 10
            return band * 10 + (bucket - lower * scale) * width // (weight * scale)
        lower += weight
    raise AssertionError("invalid score distribution")


def generate_result(*, birth_date: date, local_date: date) -> dict:
    scores = {axis: score_for(birth_date, local_date, axis) for axis in AXES}
    overall_band = f"d{min(scores['overall'] // 10, 9) * 10:02d}"
    return {
        "schema_version": 4,
        "overall": {
            "score": scores["overall"],
            "reading_code": f"overall.{overall_band}.general",
            "expression_route": f"overall.{overall_band}.general",
        },
        "categories": {
            axis: {
                "score": scores[axis],
                "reading_code": f"category.{axis}.d{min(scores[axis] // 10, 9) * 10:02d}.general.clear",
                "expression_route": f"category.{axis}.d{min(scores[axis] // 10, 9) * 10:02d}.general",
            }
            for axis in AXES[1:]
        },
        "lucky_color_key": COLORS[min(scores["overall"] // 10, 9)],
    }
