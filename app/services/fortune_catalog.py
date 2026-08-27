"""오늘의 운세 v2 다국어 seed 카탈로그 로드·검증·렌더링."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Final, Mapping
import unicodedata

from app.services import fortune_rules

_RESOURCE_DIR: Final = Path(__file__).resolve().parents[1] / "resources" / "fortune"
_MANIFEST_PATH: Final = _RESOURCE_DIR / "manifest.v2.json"
_RULES_PATH: Final = _RESOURCE_DIR / "rules.v2.json"
_COPY_FILENAMES: Final = MappingProxyType(
    {
        "ko": "copy.v2.json",
        "en": "copy.v2.en.json",
        "ja": "copy.v2.ja.json",
    }
)
SUPPORTED_LOCALES: Final = tuple(_COPY_FILENAMES)
_DECILES: Final = tuple(f"d{value:02d}" for value in range(0, 100, 10))
_FLOWS: Final = (
    "start",
    "advance",
    "focus",
    "coordinate",
    "change",
    "organize",
    "recover",
    "balance",
)
_CATEGORIES: Final = ("love", "money", "work", "energy")
_COLORS: Final = (
    "red",
    "coral",
    "orange",
    "yellow",
    "green",
    "sky",
    "blue",
    "navy",
    "purple",
    "pink",
    "white",
    "beige",
)
_PLACEHOLDER_RE: Final = re.compile(r"(?:\.\.\.|\{\{|\}\}|\bTODO\b|\bTBD\b)", re.I)
_FORBIDDEN_RE: Final = re.compile(
    r"(?:힘을 써|과욕은 금물|각별히 유의|신호에 가까워|흐름이 열려|자제해야|"
    r"될 수도.{0,80}될 수도|좋을 수도.{0,80}나쁠 수도|하는 일마다|시작하는 일마다|"
    r"무엇을 해도|기대하지 않았던 이득|재물운이 좋아)"
)
_AWKWARD_COPY_RE: Final = re.compile(
    r"(?:앞서가기보다|눈에 띄는 진전|진전을 만들|서로의 의견을 무리 없이|"
    r"판단과 여유가 잘 맞아떨어|무난하게)"
)
_OVERALL_DOMAIN_RE: Final = re.compile(
    r"(?:금전|지출|결제|연애|상대방|업무|과제|수면|몸|피로|컨디션|식사|숨을 돌)"
)
_OVERALL_DOMAIN_BY_LOCALE: Final = MappingProxyType(
    {
        "ko": _OVERALL_DOMAIN_RE,
        "ja": re.compile(
            r"(?:恋愛|恋人|デート|お金|金銭|出費|支払い|予算|買い物|価格|貯金|収入|"
            r"仕事|会社|職場|同僚|上司|業務|勉強|提出|締切|健康|体調|睡眠|食事|疲れ|心身|運動)"
        ),
        "en": re.compile(
            r"\b(?:love|romance|relationship|money|financial|spending|budget|purchase|price|"
            r"saving|income|job|career|workplace|coworker|boss|assignment|study|deadline|health|"
            r"sleep|meal|fatigue|body|workout)\b",
            re.I,
        ),
    }
)
_HANGUL_RE: Final = re.compile(r"[가-힣]")
_CJK_RE: Final = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_HEX_RE: Final = re.compile(r"#[0-9A-F]{6}")

COPY_VERSION = "fortune-copy.v2-seed.4"


class FortuneCatalogError(ValueError):
    """버전 고정 카탈로그가 런타임 계약을 위반했다."""


def _overall_keys() -> set[str]:
    return {f"overall.{decile}.{flow}.default" for decile in _DECILES for flow in _FLOWS}


def _category_keys() -> set[str]:
    return {
        f"category.{category}.{decile}.general" for category in _CATEGORIES for decile in _DECILES
    }


EXPECTED_OVERALL_KEYS = frozenset(_overall_keys())
EXPECTED_CATEGORY_KEYS = frozenset(_category_keys())


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FortuneCatalogError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(raw: bytes, filename: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FortuneCatalogError(f"invalid JSON in {filename}: {exc}") from exc
    if not isinstance(value, dict):
        raise FortuneCatalogError(f"{filename} must contain an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise FortuneCatalogError(
            f"{label} keys mismatch: missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _text(value: Any, label: str, *, locale: str, overall: bool = False) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FortuneCatalogError(f"{label} must be a trimmed non-empty string")
    if unicodedata.normalize("NFC", value) != value:
        raise FortuneCatalogError(f"{label} must use NFC normalization")
    if _PLACEHOLDER_RE.search(value):
        raise FortuneCatalogError(f"{label} contains a placeholder")
    if locale == "ko" and _FORBIDDEN_RE.search(value):
        raise FortuneCatalogError(f"{label} contains forbidden fortune wording")
    if locale == "ko" and _AWKWARD_COPY_RE.search(value):
        raise FortuneCatalogError(f"{label} contains awkward Korean fortune wording")
    if overall and _OVERALL_DOMAIN_BY_LOCALE[locale].search(value):
        raise FortuneCatalogError(f"{label} contains category-specific wording")
    if locale != "ko" and _HANGUL_RE.search(value):
        raise FortuneCatalogError(f"{label} contains Korean script")
    if locale == "en" and _CJK_RE.search(value):
        raise FortuneCatalogError(f"{label} contains CJK script")
    return value


@dataclass(frozen=True, slots=True)
class FortuneCatalog:
    overall_by_locale: Mapping[str, Mapping[str, Mapping[str, Any]]]
    categories_by_locale: Mapping[str, Mapping[str, Mapping[str, Any]]]
    colors_by_locale: Mapping[str, Mapping[str, Mapping[str, str]]]
    manifest_hash: str
    asset_hashes: Mapping[str, str]

    def render(self, semantic: Mapping[str, Any], locale: str = "ko") -> dict[str, Any]:
        if locale not in SUPPORTED_LOCALES:
            raise FortuneCatalogError(f"unsupported locale: {locale}")
        overall_catalog = self.overall_by_locale[locale]
        category_catalog = self.categories_by_locale[locale]
        color_catalog = self.colors_by_locale[locale]
        _exact_keys(
            semantic,
            {"schema_version", "overall", "categories", "lucky_color_key"},
            "semantic result",
        )
        if semantic["schema_version"] != 3:
            raise FortuneCatalogError("semantic result schema must be 3")
        overall_semantic = semantic.get("overall")
        categories_semantic = semantic.get("categories")
        color_key = semantic.get("lucky_color_key")
        if not isinstance(overall_semantic, Mapping):
            raise FortuneCatalogError("semantic overall is required")
        _exact_keys(
            overall_semantic,
            {"score", "reading_code", "expression_route"},
            "semantic overall",
        )
        if not isinstance(categories_semantic, Mapping) or set(categories_semantic) != set(
            _CATEGORIES
        ):
            raise FortuneCatalogError("semantic categories must contain four categories")
        overall_score = _score(overall_semantic.get("score"), "semantic overall score")
        overall_decile = fortune_rules.decile_for(overall_score)
        overall_reading = _overall_reading(overall_semantic.get("reading_code"))
        overall_route = overall_semantic.get("expression_route")
        expected_route = f"overall.{overall_decile}.{overall_reading['flow']}.default"
        if overall_reading["decile"] != overall_decile or overall_route != expected_route:
            raise FortuneCatalogError("semantic overall score and routes do not match")
        if overall_route not in overall_catalog:
            raise FortuneCatalogError(f"unknown overall route: {overall_route}")
        bundle = overall_catalog[str(overall_route)]
        rules = fortune_rules.load_rule_assets()
        rendered_categories: dict[str, Any] = {}
        for category in _CATEGORIES:
            item = categories_semantic[category]
            if not isinstance(item, Mapping):
                raise FortuneCatalogError(f"semantic category invalid: {category}")
            _exact_keys(
                item,
                {"score", "reading_code", "expression_route"},
                f"semantic category {category}",
            )
            score = _score(item.get("score"), f"semantic category score {category}")
            decile = fortune_rules.decile_for(score)
            reading = _category_reading(item.get("reading_code"), category)
            route = item.get("expression_route")
            allowed_topics = {str(slot["topic"]) for slot in rules["category_slots"][category]} | {
                "general"
            }
            if (
                reading["decile"] != decile
                or reading["topic"] not in allowed_topics
                or route != f"category.{category}.{decile}.general"
            ):
                raise FortuneCatalogError(
                    f"semantic category score and routes do not match: {category}"
                )
            if route not in category_catalog:
                raise FortuneCatalogError(f"unknown category route: {route}")
            rendered_categories[category] = {
                "text": list(category_catalog[str(route)]["text"]),
            }
        if color_key not in color_catalog:
            raise FortuneCatalogError(f"unknown lucky color: {color_key}")
        color_index = min(overall_score // 10, 9)
        expected_color = rules["lucky_color_by_flow"][overall_reading["flow"]][color_index]
        if color_key != expected_color:
            raise FortuneCatalogError("lucky color does not match overall flow and score")
        color = color_catalog[str(color_key)]
        return {
            "overall": {
                "headline": bundle["headline"],
                "flow": list(bundle["flow"]),
                "do": bundle["do"],
                "pause": bundle["pause"],
            },
            "categories": rendered_categories,
            "lucky_color": {"key": color_key, "name": color["name"], "hex": color["hex"]},
        }


def _score(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise FortuneCatalogError(f"{label} must be an integer in [0, 100]")
    return value


def _overall_reading(value: Any) -> dict[str, str]:
    if not isinstance(value, str):
        raise FortuneCatalogError("semantic overall reading_code must be a string")
    parts = value.split(".")
    if (
        len(parts) != 4
        or parts[0] != "overall"
        or parts[1] not in _DECILES
        or parts[2] not in _FLOWS
        or parts[3] not in {"clear", "mixed"}
    ):
        raise FortuneCatalogError("invalid semantic overall reading_code")
    return {"decile": parts[1], "flow": parts[2], "context": parts[3]}


def _category_reading(value: Any, category: str) -> dict[str, str]:
    if not isinstance(value, str):
        raise FortuneCatalogError(f"semantic category reading_code must be a string: {category}")
    parts = value.split(".")
    if (
        len(parts) != 5
        or parts[0] != "category"
        or parts[1] != category
        or parts[2] not in _DECILES
        or not parts[3]
        or parts[4] not in {"clear", "mixed"}
    ):
        raise FortuneCatalogError(f"invalid semantic category reading_code: {category}")
    return {"decile": parts[2], "topic": parts[3], "context": parts[4]}


def _validate_copy(
    asset: Mapping[str, Any],
    *,
    locale: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    _exact_keys(
        asset,
        {"schema", "copy_version", "content_status", "locales", "overall", "categories", "colors"},
        "copy asset",
    )
    if asset["schema"] != "fortune-copy-v2" or asset["copy_version"] != COPY_VERSION:
        raise FortuneCatalogError("unexpected copy schema or version")
    if asset["content_status"] != "development_seed" or asset["locales"] != [locale]:
        raise FortuneCatalogError(f"v2 seed catalog locale mismatch: {locale}")

    overall = asset["overall"]
    categories = asset["categories"]
    colors = asset["colors"]
    if (
        not isinstance(overall, dict)
        or not isinstance(categories, dict)
        or not isinstance(colors, dict)
    ):
        raise FortuneCatalogError("copy sections must be objects")
    _exact_keys(overall, set(EXPECTED_OVERALL_KEYS), "overall routes")
    _exact_keys(categories, set(EXPECTED_CATEGORY_KEYS), "category routes")
    _exact_keys(colors, set(_COLORS), "colors")

    validated_overall: dict[str, Any] = {}
    expressions: list[str] = []
    for route, bundle in overall.items():
        if not isinstance(bundle, dict):
            raise FortuneCatalogError(f"{route} must be an object")
        _exact_keys(bundle, {"headline", "flow", "do", "pause"}, route)
        flow = bundle["flow"]
        if not isinstance(flow, list) or len(flow) != 3:
            raise FortuneCatalogError(f"{route}.flow must contain exactly three sentences")
        headline = _text(bundle["headline"], f"{route}.headline", locale=locale, overall=True)
        rendered_flow = tuple(
            _text(sentence, f"{route}.flow[{index}]", locale=locale, overall=True)
            for index, sentence in enumerate(flow)
        )
        do = _text(bundle["do"], f"{route}.do", locale=locale, overall=True)
        pause = _text(bundle["pause"], f"{route}.pause", locale=locale, overall=True)
        expressions.extend((headline, *rendered_flow, do, pause))
        validated_overall[route] = MappingProxyType(
            {
                "headline": headline,
                "flow": rendered_flow,
                "do": do,
                "pause": pause,
            }
        )

    validated_categories: dict[str, Any] = {}
    for route, block in categories.items():
        if not isinstance(block, dict):
            raise FortuneCatalogError(f"{route} must be an object")
        _exact_keys(block, {"text"}, route)
        lines = block["text"]
        if not isinstance(lines, list) or len(lines) != 2:
            raise FortuneCatalogError(f"{route}.text must contain exactly two sentences")
        rendered_lines = tuple(
            _text(line, f"{route}.text[{index}]", locale=locale) for index, line in enumerate(lines)
        )
        expressions.extend(rendered_lines)
        validated_categories[route] = MappingProxyType({"text": rendered_lines})

    if len(expressions) != len(set(expressions)):
        raise FortuneCatalogError(f"all {locale} fortune expressions must be unique")

    validated_colors: dict[str, Any] = {}
    for key, color in colors.items():
        if not isinstance(color, dict):
            raise FortuneCatalogError(f"color {key} must be an object")
        _exact_keys(color, {"name", "hex"}, f"color {key}")
        name = _text(color["name"], f"color {key}.name", locale=locale)
        hex_value = color["hex"]
        if not isinstance(hex_value, str) or not _HEX_RE.fullmatch(hex_value):
            raise FortuneCatalogError(f"invalid color hex: {key}")
        validated_colors[key] = MappingProxyType({"name": name, "hex": hex_value})
    return validated_overall, validated_categories, validated_colors


def _load_catalog(resource_dir: Path) -> FortuneCatalog:
    manifest_path = resource_dir / _MANIFEST_PATH.name
    manifest_raw = manifest_path.read_bytes()
    manifest = _load_json(manifest_raw, manifest_path.name)
    _exact_keys(manifest, {"schema", "assets"}, "manifest")
    if manifest["schema"] != "fortune-manifest-v2":
        raise FortuneCatalogError("unexpected manifest schema")
    assets = manifest["assets"]
    if not isinstance(assets, dict):
        raise FortuneCatalogError("manifest assets must be an object")
    expected_assets = {_RULES_PATH.name, *_COPY_FILENAMES.values()}
    _exact_keys(assets, expected_assets, "manifest assets")
    parsed: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    for filename in sorted(expected_assets):
        metadata = assets[filename]
        if not isinstance(metadata, dict):
            raise FortuneCatalogError(f"manifest metadata invalid: {filename}")
        _exact_keys(metadata, {"sha256"}, f"manifest {filename}")
        raw = (resource_dir / filename).read_bytes()
        actual = sha256(raw).hexdigest()
        if metadata["sha256"] != actual:
            raise FortuneCatalogError(f"sha256 mismatch for {filename}")
        parsed[filename] = _load_json(raw, filename)
        hashes[filename] = actual
    overall_by_locale: dict[str, Any] = {}
    categories_by_locale: dict[str, Any] = {}
    colors_by_locale: dict[str, Any] = {}
    for locale, filename in _COPY_FILENAMES.items():
        overall, categories, colors = _validate_copy(parsed[filename], locale=locale)
        overall_by_locale[locale] = MappingProxyType(overall)
        categories_by_locale[locale] = MappingProxyType(categories)
        colors_by_locale[locale] = MappingProxyType(colors)
    return FortuneCatalog(
        overall_by_locale=MappingProxyType(overall_by_locale),
        categories_by_locale=MappingProxyType(categories_by_locale),
        colors_by_locale=MappingProxyType(colors_by_locale),
        manifest_hash=sha256(manifest_raw).hexdigest(),
        asset_hashes=MappingProxyType(hashes),
    )


@lru_cache(maxsize=1)
def _load_default_catalog() -> FortuneCatalog:
    return _load_catalog(_RESOURCE_DIR)


def load_catalog(resource_dir: Path | None = None) -> FortuneCatalog:
    return _load_default_catalog() if resource_dir is None else _load_catalog(Path(resource_dir))


def render_all(semantic: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """개발 seed가 지원하는 모든 언어의 고정 snapshot을 반환한다."""

    catalog = load_catalog()
    return {locale: catalog.render(semantic, locale) for locale in SUPPORTED_LOCALES}
