from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.services import fortune_ephemeris

_FIXTURE = Path(__file__).parent / "fixtures" / "fortune_ephemeris.json"


def test_official_library_regression_fixture_is_explicitly_not_jpl_validation():
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert fixture["fixture_kind"] == "astronomy_engine_library_regression"
    assert fixture["library_version"] == "2.1.19"
    assert "not an independent JPL Horizons" in fixture["provenance"]


def test_geocentric_longitudes_match_pinned_library_regression_fixture():
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    tolerance = fixture["tolerance_degrees"]
    for case in fixture["cases"]:
        when = datetime.fromisoformat(case["utc"].replace("Z", "+00:00"))
        actual = fortune_ephemeris.geocentric_ecliptic_longitudes(when)
        assert tuple(actual) == fortune_ephemeris.PLANET_KEYS
        for planet, expected in case["longitudes"].items():
            assert actual[planet] == pytest.approx(expected, abs=tolerance)


def test_wrapper_uses_geovector_then_ecliptic_and_never_convenience_longitude(monkeypatch):
    calls: list[tuple] = []

    class _Coordinates:
        elon = 361.25

    def fake_geo(body, time, aberration):
        calls.append(("geo", body, aberration))
        return object()

    def fake_ecliptic(vector):
        calls.append(("ecliptic", vector))
        return _Coordinates()

    def forbidden(*_args, **_kwargs):  # pragma: no cover - only runs on regression
        raise AssertionError("EclipticLongitude must not be used")

    monkeypatch.setattr(fortune_ephemeris.astronomy, "GeoVector", fake_geo)
    monkeypatch.setattr(fortune_ephemeris.astronomy, "Ecliptic", fake_ecliptic)
    monkeypatch.setattr(fortune_ephemeris.astronomy, "EclipticLongitude", forbidden)
    result = fortune_ephemeris.geocentric_ecliptic_longitude(
        "sun", datetime(2026, 8, 27, tzinfo=timezone.utc)
    )
    assert result == 1.25
    assert [call[0] for call in calls] == ["geo", "ecliptic"]
    assert calls[0][2] is True


def test_ephemeris_rejects_naive_time_unknown_planet_and_bad_longitude():
    with pytest.raises(fortune_ephemeris.EphemerisInputError):
        fortune_ephemeris.geocentric_ecliptic_longitude("sun", datetime(2026, 1, 1))
    with pytest.raises(fortune_ephemeris.EphemerisInputError):
        fortune_ephemeris.geocentric_ecliptic_longitude(
            "earth", datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
    with pytest.raises(fortune_ephemeris.EphemerisInputError):
        fortune_ephemeris.angular_separation(-1, 10)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [(359.0, 1.0, 2.0), (10.0, 190.0, 180.0), (42.0, 42.0, 0.0)],
)
def test_angular_separation_wraps_at_360(left, right, expected):
    assert fortune_ephemeris.angular_separation(left, right) == expected


def test_circular_mean_crosses_zero_without_falling_back_to_180():
    assert fortune_ephemeris.circular_mean_longitudes([359.0, 1.0]) == pytest.approx(0.0)
    with pytest.raises(fortune_ephemeris.EphemerisInputError):
        fortune_ephemeris.circular_mean_longitudes([0.0, 180.0])


def test_date_chart_uses_four_local_day_samples_and_returns_copy(monkeypatch):
    fortune_ephemeris._date_chart_cache.cache_clear()
    calls: list[datetime] = []

    def fake_longitude(_planet: str, instant: datetime) -> float:
        calls.append(instant)
        return float(instant.hour)

    monkeypatch.setattr(fortune_ephemeris, "geocentric_ecliptic_longitude", fake_longitude)
    first = fortune_ephemeris.date_chart_longitudes(date(2026, 8, 27), "UTC", ("sun",))
    first["sun"] = 99.0
    second = fortune_ephemeris.date_chart_longitudes(date(2026, 8, 27), "UTC", ("sun",))
    assert [instant.hour for instant in calls] == [0, 6, 12, 18]
    assert second["sun"] != 99.0
    assert tuple(second) == ("sun",)
