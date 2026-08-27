"""오늘의 운세가 사용하는 Astronomy Engine 좌표 경계.

이 모듈은 점성술 해석을 하지 않는다. 입력 UTC 시각에 대해 지구 중심 apparent vector를 구한 뒤
true ecliptic-of-date 황경으로 변환하는 단일 경로만 공개한다. 태양 중심 좌표를 실수로 섞지 않도록
``HelioVector``와 ``EclipticLongitude``는 사용하지 않는다.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from functools import lru_cache
import math
from types import MappingProxyType
from typing import Final, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import astronomy

EPHEMERIS_VERSION: Final = "astronomy-engine-2.1.19-geocentric-apparent-v1"

PLANET_KEYS: Final[tuple[str, ...]] = (
    "sun",
    "moon",
    "mercury",
    "venus",
    "mars",
    "jupiter",
    "saturn",
)

BIRTH_PLANET_KEYS: Final[tuple[str, ...]] = (
    "sun",
    "mercury",
    "venus",
    "mars",
    "jupiter",
    "saturn",
)

_BODIES: Final[Mapping[str, astronomy.Body]] = MappingProxyType(
    {
        "sun": astronomy.Body.Sun,
        "moon": astronomy.Body.Moon,
        "mercury": astronomy.Body.Mercury,
        "venus": astronomy.Body.Venus,
        "mars": astronomy.Body.Mars,
        "jupiter": astronomy.Body.Jupiter,
        "saturn": astronomy.Body.Saturn,
    }
)


class EphemerisInputError(ValueError):
    """호출자가 천문 계산 경계를 위반했다."""


def _time(when_utc: datetime) -> astronomy.Time:
    if when_utc.tzinfo is None or when_utc.utcoffset() is None:
        raise EphemerisInputError("when_utc must be timezone-aware")
    value = when_utc.astimezone(timezone.utc)
    second = value.second + value.microsecond / 1_000_000
    return astronomy.Time.Make(
        value.year, value.month, value.day, value.hour, value.minute, second
    )


def geocentric_ecliptic_longitude(planet: str, when_utc: datetime) -> float:
    """지구에서 본 천체의 true ecliptic-of-date 황경을 ``[0, 360)``으로 반환한다."""

    key = planet.strip().lower() if isinstance(planet, str) else ""
    body = _BODIES.get(key)
    if body is None:
        raise EphemerisInputError(f"unsupported planet: {planet!r}")
    # aberration=True: 빛의 이동 시간과 stellar aberration을 반영한 apparent 위치.
    vector = astronomy.GeoVector(body, _time(when_utc), True)
    longitude = float(astronomy.Ecliptic(vector).elon) % 360.0
    if not 0.0 <= longitude < 360.0:  # pragma: no cover - library contract guard
        raise RuntimeError("Astronomy Engine returned an invalid longitude")
    return longitude


def geocentric_ecliptic_longitudes(
    when_utc: datetime, planets: Sequence[str] = PLANET_KEYS
) -> dict[str, float]:
    """요청 순서와 무관하게 canonical planet 순서의 황경 mapping을 반환한다."""

    requested = set(planets)
    unknown = requested.difference(PLANET_KEYS)
    if unknown:
        raise EphemerisInputError(f"unsupported planets: {sorted(unknown)!r}")
    return {
        key: geocentric_ecliptic_longitude(key, when_utc)
        for key in PLANET_KEYS
        if key in requested
    }


def angular_separation(longitude_a: float, longitude_b: float) -> float:
    """두 황경의 원 위 최소 각거리 ``0...180``을 반환한다."""

    for value in (longitude_a, longitude_b):
        if not isinstance(value, (int, float)) or not 0.0 <= float(value) < 360.0:
            raise EphemerisInputError("longitude must be in [0, 360)")
    delta = abs(float(longitude_a) - float(longitude_b)) % 360.0
    return min(delta, 360.0 - delta)


def circular_mean_longitudes(values: Sequence[float]) -> float:
    """원 경계를 안전하게 처리한 황경 평균을 ``[0, 360)``으로 반환한다."""

    if not values:
        raise EphemerisInputError("at least one longitude is required")
    radians: list[float] = []
    for value in values:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) < 360.0
        ):
            raise EphemerisInputError("longitude must be finite and in [0, 360)")
        radians.append(math.radians(float(value)))
    x = sum(math.cos(value) for value in radians)
    y = sum(math.sin(value) for value in radians)
    if math.hypot(x, y) < 1e-12:
        raise EphemerisInputError("circular mean is undefined for opposite longitudes")
    result = math.degrees(math.atan2(y, x)) % 360.0
    # 359°와 1°처럼 정확한 평균이 0°인 입력은 부동소수점 오차로 360.0이 될 수 있다.
    return 0.0 if math.isclose(result, 360.0, abs_tol=1e-12) else result


@lru_cache(maxsize=4096)
def _date_chart_cache(
    local_date: date,
    timezone_name: str,
    planets: tuple[str, ...],
) -> tuple[tuple[str, float], ...]:
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise EphemerisInputError(f"invalid IANA timezone: {timezone_name!r}") from exc
    samples = [
        datetime.combine(local_date, time(hour=hour), tzinfo=zone).astimezone(timezone.utc)
        for hour in (0, 6, 12, 18)
    ]
    return tuple(
        (
            planet,
            circular_mean_longitudes(
                [geocentric_ecliptic_longitude(planet, instant) for instant in samples]
            ),
        )
        for planet in planets
    )


def date_chart_longitudes(
    local_date: date,
    timezone_name: str,
    planets: Sequence[str] = PLANET_KEYS,
) -> dict[str, float]:
    """현지 날짜 00·06·12·18시의 원형 평균으로 하루 기준 황경을 만든다.

    출생일 기준은 ``timezone_name='UTC'``와 :data:`BIRTH_PLANET_KEYS`를 사용한다. 오늘
    기준은 사용자 현지 시간대와 :data:`PLANET_KEYS`를 사용한다. 캐시 내부값은 불변 tuple로
    보관하고 호출자에게 새 dict를 돌려주므로 사용자 코드가 공용 캐시를 변형할 수 없다.
    """

    if isinstance(local_date, datetime) or not isinstance(local_date, date):
        raise EphemerisInputError("local_date must be a date")
    requested = set(planets)
    unknown = requested.difference(PLANET_KEYS)
    if unknown:
        raise EphemerisInputError(f"unsupported planets: {sorted(unknown)!r}")
    canonical = tuple(key for key in PLANET_KEYS if key in requested)
    if not canonical:
        raise EphemerisInputError("at least one planet is required")
    return dict(_date_chart_cache(local_date, timezone_name, canonical))
