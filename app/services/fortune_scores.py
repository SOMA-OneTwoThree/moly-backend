"""Deterministic experience scores conditioned on the existing astrology engine.

Astronomical aspects are retained as internal provenance and seed input, not as
scientific predictions or a claim that harmonious aspects imply higher scores.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
from functools import lru_cache
from hashlib import sha256
import json

from app.services import fortune_ephemeris, fortune_rules

RULE_VERSION = "fortune-experience.v1"
EPHEMERIS_VERSION = fortune_ephemeris.EPHEMERIS_VERSION
AXES = ("overall", "love", "money", "work", "energy")
# Retained for reading older persisted schema-4 results only.
COLORS = ("white", "beige", "sky", "blue", "green", "green", "yellow", "orange", "pink", "red")
# Integer approximation of exp(-offset**2 / (2 * 5.5**2)). A slight
# rightward tilt compensates for the bounded upper tail and rare low scores.
_GAUSSIAN = (1000, 984, 936, 862, 768, 661, 552, 445, 347, 262,
             191, 135, 93, 61, 39, 24, 14, 8, 5)
REGULAR_WEIGHTS = tuple(_GAUSSIAN[abs(s - 88)] * (1000 + 8 * (s - 88))
                        for s in range(70, 101))
FIRST_WEIGHTS = (14, 28, 49, 73, 92, 100, 92, 73, 49, 28, 14)


def _validate(birth_date: date, local_date: date, first_visit: bool) -> None:
    if type(birth_date) is not date or type(local_date) is not date:
        raise ValueError("fortune dates must be calendar dates")
    if not date(1900, 1, 1) <= birth_date <= local_date:
        raise ValueError("invalid fortune birth date")
    if type(first_visit) is not bool:
        raise ValueError("first_visit must be boolean")


@lru_cache(maxsize=4096)
def _astrology_json(birth_date: date, local_date: date) -> str:
    birth = fortune_ephemeris.date_chart_longitudes(
        birth_date, "UTC", fortune_ephemeris.BIRTH_PLANET_KEYS,
    )
    current = fortune_ephemeris.date_chart_longitudes(local_date, "UTC")
    semantic = fortune_rules.generate_semantic_result(
        birth_positions=birth, current_positions=current,
    )
    signals = fortune_rules.select_top_three(fortune_rules.detect_signals(
        birth_positions=birth, current_positions=current,
    ))
    return json.dumps({
        "ephemeris_version": EPHEMERIS_VERSION,
        "rule_version": fortune_rules.RULE_VERSION,
        "reference_timezone": "UTC",
        "semantic": semantic,
        "top_signals": [asdict(signal) for signal in signals],
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _draw(seed: str, domain: str, size: int) -> int:
    # Rejection sampling makes the integer draw unbiased, including the 1/100
    # result-wide low gate. Domain separation prevents axis-order dependence.
    scale = 1 << 256
    limit = scale - scale % size
    counter = 0
    while True:
        value = int.from_bytes(sha256(f"{seed}|{domain}|{counter}".encode("ascii")).digest(), "big")
        if value < limit:
            return value % size
        counter += 1


def _weighted(seed: str, domain: str, start: int, weights: tuple[int, ...]) -> int:
    value = _draw(seed, domain, sum(weights))
    for offset, weight in enumerate(weights):
        if value < weight:
            return start + offset
        value -= weight
    raise AssertionError("invalid score distribution")


def _scores(birth_date: date, local_date: date, first_visit: bool, astrology: str) -> dict[str, int]:
    mode = "first_visit" if first_visit else "regular"
    seed = f"{RULE_VERSION}|{birth_date.isoformat()}|{local_date.isoformat()}|{mode}|{astrology}"
    low_axis = None
    if not first_visit and _draw(seed, "low-result", 100) == 0:
        low_axis = AXES[_draw(seed, "low-axis", len(AXES))]
    return {
        axis: (60 + _draw(seed, f"low-score:{axis}", 10) if axis == low_axis
               else _weighted(seed, f"score:{axis}", 90 if first_visit else 70,
                              FIRST_WEIGHTS if first_visit else REGULAR_WEIGHTS))
        for axis in AXES
    }


def score_for(birth_date: date, local_date: date, axis: str, *, first_visit: bool = False) -> int:
    _validate(birth_date, local_date, first_visit)
    if axis not in AXES:
        raise ValueError("unknown fortune axis")
    return _scores(birth_date, local_date, first_visit, _astrology_json(birth_date, local_date))[axis]


def generate_result(*, birth_date: date, local_date: date, first_visit: bool = False) -> dict:
    _validate(birth_date, local_date, first_visit)
    astrology_json = _astrology_json(birth_date, local_date)
    astrology = json.loads(astrology_json)
    scores = _scores(birth_date, local_date, first_visit, astrology_json)
    overall_band = f"d{min(scores['overall'] // 10, 9) * 10:02d}"
    color = astrology["semantic"]["lucky_color_key"]
    return {
        "schema_version": 4,
        "experience_version": RULE_VERSION,
        "experience_mode": "first_visit" if first_visit else "regular",
        "astrology": astrology,
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
        "lucky_color_key": "orange" if color == "coral" else color,
    }
