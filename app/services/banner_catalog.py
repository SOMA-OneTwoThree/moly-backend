"""Bounded, process-local banner definitions; no user data is cached here."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from pydantic import ValidationError

from app.schemas.banners import (
    BannerAuthoredCanvas,
    BannerCanvas,
    BannerCard,
    BannerDefinition,
    BannerFeed,
    BannerManifest,
    version_core,
)

CATALOG_PATH = Path(__file__).resolve().parents[1] / "resources/banners/home_blind.json"
MAX_MANIFEST_BYTES = 256 * 1024
MAX_FEED_BYTES = 128 * 1024


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-finite JSON constant")


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, tuple):
        return tuple(_freeze(v) for v in value)
    if hasattr(type(value), "model_fields"):
        for name in type(value).model_fields:
            object.__setattr__(value, name, _freeze(getattr(value, name)))
    return value


@dataclass(frozen=True)
class BannerCatalog:
    revision: str
    manifest: BannerManifest

    @classmethod
    def from_bytes(cls, raw: bytes) -> BannerCatalog:
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError("manifest byte budget exceeded")
        json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
        manifest = BannerManifest.model_validate_json(raw)
        # Validate each expression branch against the public schema, including text limits.
        for banner in manifest.banners:
            for locale, canvas in banner.canvases_by_locale.items():
                for count in (0, 1, 999999999):
                    values = binding_values(banner, locale, date(2026, 12, 31), count)
                    compile_canvas(canvas, values)
        return cls(hashlib.sha256(raw).hexdigest(), _freeze(manifest))

    @classmethod
    def load(cls, path: Path = CATALOG_PATH) -> BannerCatalog:
        with path.open("rb") as stream:
            return cls.from_bytes(stream.read(MAX_MANIFEST_BYTES + 1))


def capabilities(canvas: BannerAuthoredCanvas) -> frozenset[str]:
    result = {"banner_canvas_v1", "home_blind_v1", canvas.background.type}
    for element in canvas.elements:
        result.add(element.type)
        if element.type == "button_v1":
            result.add(element.action.type)
    return frozenset(result)


def select_candidates(
    catalog: BannerCatalog,
    *,
    now: datetime,
    platform: str,
    app_version: str,
    locale: str,
    supported: frozenset[str],
):
    if not catalog.manifest.enabled:
        return ()
    version = version_core(app_version)
    selected = []
    for banner in catalog.manifest.banners:
        if not banner.enabled or platform not in banner.platforms:
            continue
        if banner.starts_at and now < banner.starts_at:
            continue
        if banner.ends_at and now >= banner.ends_at:
            continue
        lower, upper = banner.min_app_version, banner.max_app_version_exclusive
        if (lower or upper) and version is None:
            continue
        if lower and version < version_core(lower):
            continue
        if upper and version >= version_core(upper):
            continue
        chosen_locale = locale if locale in banner.canvases_by_locale else "en"
        canvas = banner.canvases_by_locale[chosen_locale]
        if capabilities(canvas) <= supported:
            selected.append((banner, chosen_locale, canvas))
    return tuple(selected)


def binding_values(
    banner: BannerDefinition, locale: str, local_date: date | None, remaining: int | None
) -> dict[str, str | int]:
    values = {}
    for alias, binding in banner.bindings.items():
        if binding.source == "routines.remaining_today":
            if remaining is None:
                raise ValueError("routine binding unavailable")
            values[alias] = remaining
        else:
            if local_date is None:
                raise ValueError("date binding unavailable")
            if binding.format == "full_date":
                values[alias] = local_date.isoformat()
            elif locale == "ko":
                values[alias] = f"{local_date.month}월 {local_date.day}일"
            elif locale == "ja":
                values[alias] = f"{local_date.month}月{local_date.day}日"
            else:
                months = (
                    "Jan",
                    "Feb",
                    "Mar",
                    "Apr",
                    "May",
                    "Jun",
                    "Jul",
                    "Aug",
                    "Sep",
                    "Oct",
                    "Nov",
                    "Dec",
                )
                values[alias] = f"{months[local_date.month - 1]} {local_date.day}"
    return values


def compile_canvas(canvas: BannerAuthoredCanvas, values: Mapping[str, str | int]) -> BannerCanvas:
    raw = canvas.model_dump(mode="json")
    for element in raw["elements"]:
        if element["type"] == "image_v1":
            continue
        expression = element["text"]
        if expression["kind"] == "count_cases":
            count = values[expression["binding"]]
            expression = expression["zero" if count == 0 else "one" if count == 1 else "other"]
        element["text"] = expression["value"].format_map(values)
    return BannerCanvas.model_validate_json(json.dumps(raw, ensure_ascii=False))


def render_feed(
    catalog: BannerCatalog,
    candidates,
    *,
    now: datetime,
    local_date: date | None,
    day_ends_at: datetime | None,
    remaining: int | None,
) -> BannerFeed:
    cards = []
    for banner, locale, canvas in candidates:
        try:
            values = binding_values(banner, locale, local_date, remaining)
            if banner.when:
                value = values[banner.when.binding]
                if banner.when.operator == "eq" and value != banner.when.value:
                    continue
                if banner.when.operator == "gt" and value <= banner.when.value:
                    continue
            deadlines = [banner.ends_at]
            if banner.bindings:
                deadlines.append(day_ends_at)
            deadline = min((v for v in deadlines if v is not None), default=None)
            card = BannerCard(
                id=banner.id,
                component=banner.component,
                layout_profile=banner.layout_profile,
                locale=locale,
                valid_until=deadline,
                canvas=compile_canvas(canvas, values),
            )
            cards.append(card)
        except (ValueError, KeyError, ValidationError):
            continue
        if len(cards) == 5:
            break
    result = BannerFeed(
        schema_version=1,
        placement="home_blind",
        revision=catalog.revision,
        served_at=now,
        items=tuple(cards),
    )
    if len(result.model_dump_json().encode("utf-8")) > MAX_FEED_BYTES:
        raise ValueError("response byte budget exceeded")
    return result
