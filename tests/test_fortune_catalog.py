from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
import shutil

import pytest

from app.services.fortune_catalog import (
    CONTENT_STATUS,
    COPY_VERSION,
    COPY_VERSIONS,
    EXPECTED_CATEGORY_KEYS,
    EXPECTED_OVERALL_KEYS,
    FortuneCatalogError,
    SUPPORTED_LOCALES,
    load_catalog,
    render_all,
)

from app.services.fortune_copy_selection import FortuneSelectionError, overall_copy_route


RESOURCE_DIR = Path(__file__).resolve().parents[1] / "app" / "resources" / "fortune"


def _copy_resources(tmp_path: Path) -> Path:
    target = tmp_path / "fortune"
    shutil.copytree(RESOURCE_DIR, target)
    return target


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _refresh_manifest_hash(resource_dir: Path, filename: str) -> None:
    manifest_path = resource_dir / "manifest.v2.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["assets"][filename]["sha256"] = hashlib.sha256(
        (resource_dir / filename).read_bytes()
    ).hexdigest()
    _write_json(manifest_path, manifest)


def _selected(variant: str = "v01") -> dict[str, str]:
    return {key: variant for key in ("overall", "love", "money", "work", "energy")}


def _semantic() -> dict:
    return {
        "schema_version": 3,
        "overall": {
            "score": 65,
            "reading_code": "overall.d60.recover.clear",
            "expression_route": "overall.d60.recover.default",
        },
        "categories": {
            category: {
                "score": 50,
                "reading_code": f"category.{category}.d50.general.clear",
                "expression_route": f"category.{category}.d50.general",
            }
            for category in ("love", "money", "work", "energy")
        },
        "lucky_color_key": "blue",
    }


def test_approved_catalog_has_complete_variant_coverage():
    catalog = load_catalog()
    assert COPY_VERSION == "fortune-copy.v3-editorial.1"
    assert CONTENT_STATUS == "approved_for_production"
    assert SUPPORTED_LOCALES == ("ko", "en", "ja")
    for locale in SUPPORTED_LOCALES:
        assert set(catalog.overall_by_locale[locale]) == set(EXPECTED_OVERALL_KEYS)
        assert set(catalog.categories_by_locale[locale]) == set(EXPECTED_CATEGORY_KEYS)
        assert len(catalog.overall_by_locale[locale]) == 10
        assert len(catalog.categories_by_locale[locale]) == 40
        assert len(catalog.colors_by_locale[locale]) == 12
        filename = {"ko": "copy.v2.json", "en": "copy.v2.en.json", "ja": "copy.v2.ja.json"}[locale]
        asset = json.loads((RESOURCE_DIR / filename).read_text(encoding="utf-8"))
        assert asset["content_status"] == CONTENT_STATUS
        assert asset["copy_version"] == COPY_VERSIONS[locale]
    assert len(catalog.manifest_hash) == 64


def test_unapproved_copy_is_rejected_even_when_manifest_hash_matches(tmp_path):
    resources = _copy_resources(tmp_path)
    path = resources / "copy.v2.json"
    asset = json.loads(path.read_text(encoding="utf-8"))
    asset["content_status"] = "development_seed"
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)

    with pytest.raises(FortuneCatalogError, match="approval/locale"):
        load_catalog(resources)


@pytest.mark.parametrize("locale,filename", [
    ("ko", "copy.v2.json"), ("en", "copy.v2.en.json"), ("ja", "copy.v2.ja.json"),
])
def test_complete_day_readings_are_distinct_without_forcing_action_synonyms(locale, filename):
    asset = json.loads((RESOURCE_DIR / filename).read_text())
    assert asset["locales"] == [locale]
    headlines, readings, expressions = [], [], []
    for wrapper in asset["overall"].values():
        assert set(wrapper) == {"variants"}
        assert set(wrapper["variants"]) == {f"v{i:02d}" for i in range(1, 21)}
        pool = []
        for b in wrapper["variants"].values():
            assert set(b) == {"headline", "flow", "do", "pause"}
            assert len(b["flow"]) == 3
            assert len(re.findall(r"[.!?。！？]", " ".join(b["flow"]))) == 5
            assert all("\n" not in text and "\r" not in text for text in b["flow"])
            texts = [b["headline"], *b["flow"], b["do"], b["pause"]]
            assert all(text.endswith((".", "。", "!", "?", "！", "？")) for text in texts)
            if locale == "ko":
                assert all("겠어" not in text for text in texts)
            headlines.append(b["headline"])
            reading = " ".join(texts[:4])
            pool.append(reading)
            readings.append(reading)
            expressions.extend(texts)
        # Detect copied readings, not shared everyday words or short advice labels.
        for i, left in enumerate(pool):
            for right in pool[i + 1:]:
                assert SequenceMatcher(None, left, right).ratio() < 0.85
    assert len(headlines) == len(set(headlines)) == 200
    assert len(readings) == len(set(readings)) == 200
    for wrapper in asset["categories"].values():
        assert len(wrapper["variants"]) == 20
        for b in wrapper["variants"].values():
            assert len(b["text"]) == 2
            assert len(re.findall(r"[.!?。！？]", " ".join(b["text"]))) == 5
            assert all("\n" not in text and "\r" not in text for text in b["text"])
            assert all(text.endswith((".", "。", "!", "?", "！", "？")) for text in b["text"])
            if locale == "ko":
                assert all(
                    not any(token in text for token in ("겠어", "흐름이야", "가능성이 보여", "기운이 모여"))
                    for text in b["text"]
                )
            expressions.extend(b["text"])
    assert len(expressions) == 2800


@pytest.mark.parametrize("text", ["오늘은 마음이 편해지겠어", "오늘은 한결 편안한 흐름이야."])
def test_retired_overall_tone_is_rejected(tmp_path, text):
    resources = _copy_resources(tmp_path)
    path = resources / "copy.v2.json"
    asset = json.loads(path.read_text())
    asset["overall"]["overall.d00.general"]["variants"]["v01"]["headline"] = text
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError, match="retired fortune"):
        load_catalog(resources)


def test_manifest_hashes_rules_and_copy_together():
    catalog = load_catalog()
    manifest = json.loads((RESOURCE_DIR / "manifest.v2.json").read_text(encoding="utf-8"))
    for filename in ("rules.v2.json", "copy.v2.json", "copy.v2.en.json", "copy.v2.ja.json"):
        actual = hashlib.sha256((RESOURCE_DIR / filename).read_bytes()).hexdigest()
        assert manifest["assets"][filename]["sha256"] == actual
        assert catalog.asset_hashes[filename] == actual


def test_render_returns_atomic_localized_bundles_and_four_two_line_categories():
    rendered = render_all(_semantic(), selected=_selected())
    assert set(rendered) == {"ko", "en", "ja"}
    for result in rendered.values():
        assert set(result["overall"]) == {"headline", "flow", "do", "pause"}
        assert len(result["overall"]["flow"]) == 3
        assert set(result["categories"]) == {"love", "money", "work", "energy"}
        assert all(len(value["text"]) == 2 for value in result["categories"].values())
    assert rendered["ko"]["lucky_color"] == {"key": "blue", "name": "파랑", "hex": "#1E88E5"}
    assert rendered["en"]["lucky_color"]["name"] == "Blue"
    assert rendered["ja"]["lucky_color"]["name"] == "青"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(schema_version=2), "schema must be 3"),
        (
            lambda value: value["overall"].update(expression_route="overall.d00.recover.default"),
            "overall score and routes",
        ),
        (
            lambda value: value["categories"]["love"].update(score=95),
            "category score and routes",
        ),
        (
            lambda value: value["categories"]["love"].update(
                reading_code="category.love.d50.invented.clear"
            ),
            "category score and routes",
        ),
        (lambda value: value.update(lucky_color_key="red"), "lucky color does not match"),
    ],
)
def test_semantic_score_route_and_color_mismatches_fail_closed(mutate, message):
    semantic = _semantic()
    mutate(semantic)
    with pytest.raises(FortuneCatalogError, match=message):
        render_all(semantic, selected=_selected())


def test_missing_route_is_rejected_even_with_a_valid_hash(tmp_path: Path):
    resources = _copy_resources(tmp_path)
    path = resources / "copy.v2.json"
    asset = json.loads(path.read_text(encoding="utf-8"))
    asset["overall"].pop("overall.d00.general")
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError, match="overall routes keys mismatch"):
        load_catalog(resources)


@pytest.mark.parametrize("bad", ["힘을 써", "오늘은 신호에 가까워", "좋을 수도 나쁠 수도 있어"])
def test_forbidden_korean_copy_is_rejected(tmp_path: Path, bad: str):
    resources = _copy_resources(tmp_path)
    path = resources / "copy.v2.json"
    asset = json.loads(path.read_text(encoding="utf-8"))
    asset["overall"]["overall.d00.general"]["variants"]["v01"]["headline"] = bad
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError, match="forbidden fortune wording"):
        load_catalog(resources)


@pytest.mark.parametrize(
    "bad",
    ["앞서가기보다", "눈에 띄는 진전을 만들 수 있어", "판단과 여유가 잘 맞아떨어"],
)
def test_awkward_korean_copy_is_rejected(tmp_path: Path, bad: str):
    resources = _copy_resources(tmp_path)
    path = resources / "copy.v2.json"
    asset = json.loads(path.read_text(encoding="utf-8"))
    asset["overall"]["overall.d00.general"]["variants"]["v01"]["headline"] = bad
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError, match="awkward Korean fortune wording"):
        load_catalog(resources)


def test_duplicate_category_bundle_is_rejected_across_routes(tmp_path: Path):
    resources = _copy_resources(tmp_path)
    path = resources / "copy.v2.json"
    asset = json.loads(path.read_text(encoding="utf-8"))
    asset["categories"]["category.money.d00.general"]["variants"]["v01"]["text"] = asset["categories"][
        "category.love.d00.general"
    ]["variants"]["v01"]["text"]
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError, match="readings must be unique"):
        load_catalog(resources)


def test_overall_category_word_and_bad_flow_length_are_rejected(tmp_path: Path):
    resources = _copy_resources(tmp_path)
    path = resources / "copy.v2.json"
    asset = json.loads(path.read_text(encoding="utf-8"))
    asset["overall"]["overall.d00.general"]["variants"]["v01"]["headline"] = "오늘은 지출을 조심하는 게 좋아"
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError, match="category-specific"):
        load_catalog(resources)

    resources = _copy_resources(tmp_path / "flow")
    path = resources / "copy.v2.json"
    asset = json.loads(path.read_text(encoding="utf-8"))
    asset["overall"]["overall.d00.general"]["variants"]["v01"]["flow"].pop()
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError, match="exactly three"):
        load_catalog(resources)


@pytest.mark.parametrize(
    ("filename", "bad", "message"),
    [
        ("copy.v2.en.json", "Money should be your only concern today", "category-specific"),
        ("copy.v2.ja.json", "今日は業務だけに集中したい日です", "category-specific"),
        ("copy.v2.en.json", "오늘은 a clear day", "Korean script"),
        ("copy.v2.en.json", "今日は a clear day", "CJK script"),
    ],
)
def test_non_korean_catalog_rejects_domain_leaks_and_wrong_scripts(
    tmp_path: Path,
    filename: str,
    bad: str,
    message: str,
):
    resources = _copy_resources(tmp_path)
    path = resources / filename
    asset = json.loads(path.read_text(encoding="utf-8"))
    asset["overall"]["overall.d00.general"]["variants"]["v01"]["headline"] = bad
    _write_json(path, asset)
    _refresh_manifest_hash(resources, filename)
    with pytest.raises(FortuneCatalogError, match=message):
        load_catalog(resources)


def test_duplicate_json_key_is_rejected_before_schema_validation(tmp_path: Path):
    resources = _copy_resources(tmp_path)
    path = resources / "copy.v2.json"
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw.replace("{\n", '{\n  "schema": "fortune-copy-v2",\n', 1), encoding="utf-8")
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError, match="duplicate JSON key: schema"):
        load_catalog(resources)


@pytest.mark.parametrize("section,route", [
    ("overall", "overall.d00.general"),
    ("categories", "category.love.d00.general"),
])
@pytest.mark.parametrize("change", ["missing", "extra"])
def test_variant_coverage_is_exact_even_with_matching_manifest(tmp_path, section, route, change):
    resources = _copy_resources(tmp_path)
    path = resources / "copy.v2.json"
    asset = json.loads(path.read_text(encoding="utf-8"))
    variants = asset[section][route]["variants"]
    if change == "missing":
        variants.pop("v20")
    else:
        variants["v21"] = variants["v01"]
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError):
        load_catalog(resources)


@pytest.mark.parametrize("variant", [f"v{index:02d}" for index in range(1, 21)])
def test_explicit_variant_selects_same_atomic_bundle_in_all_locales(variant):
    semantic = _semantic()
    rendered = render_all(semantic, selected=_selected(variant))
    for locale, filename in (("ko", "copy.v2.json"), ("en", "copy.v2.en.json"), ("ja", "copy.v2.ja.json")):
        asset = json.loads((RESOURCE_DIR / filename).read_text(encoding="utf-8"))
        overall_route = overall_copy_route(semantic["overall"]["expression_route"])
        assert rendered[locale]["overall"] == asset["overall"][overall_route]["variants"][variant]
        for category in ("love", "money", "work", "energy"):
            route = semantic["categories"][category]["expression_route"]
            assert rendered[locale]["categories"][category] == asset["categories"][route]["variants"][variant]


@pytest.mark.parametrize("selected", [
    {},
    {"overall": "v01"},
    {**_selected(), "unknown": "v01"},
    {**_selected(), "overall": "v00"},
    {**_selected(), "money": "v21"},
    {**_selected(), "work": 1},
    {**_selected(), "energy": None},
])
def test_invalid_variant_selection_is_rejected(selected):
    with pytest.raises(FortuneSelectionError):
        render_all(_semantic(), selected=selected)


def test_legacy_coral_slot_displays_gray_with_matching_hex_in_all_locales():
    catalog = load_catalog()
    colors = {locale: catalog.colors_by_locale[locale]["coral"] for locale in SUPPORTED_LOCALES}
    assert {locale: color["name"] for locale, color in colors.items()} == {
        "ko": "회색", "en": "Gray", "ja": "グレー",
    }
    assert len({color["hex"] for color in colors.values()}) == 1
    assert colors["ko"]["hex"] != "#FF7F6E"



def test_duplicate_variant_json_key_is_rejected_before_schema_validation(tmp_path):
    resources = _copy_resources(tmp_path)
    path = resources / "copy.v2.json"
    raw = path.read_text(encoding="utf-8")
    assert '"v01": {' in raw
    path.write_text(raw.replace('"v01": {', '"v01": {}, "v01": {', 1), encoding="utf-8")
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError, match="duplicate JSON key: v01"):
        load_catalog(resources)


@pytest.mark.parametrize('flow', ['start', 'advance', 'focus', 'coordinate', 'change', 'organize', 'recover', 'balance'])
def test_overall_copy_is_independent_of_internal_calculation_flow(flow):
    from app.services import fortune_rules

    semantic = _semantic()
    reference = render_all(semantic, selected=_selected('v07'))
    semantic['overall']['reading_code'] = f'overall.d60.{flow}.clear'
    semantic['overall']['expression_route'] = f'overall.d60.{flow}.default'
    semantic['lucky_color_key'] = fortune_rules.load_rule_assets()['lucky_color_by_flow'][flow][6]
    actual = render_all(semantic, selected=_selected('v07'))
    for locale in SUPPORTED_LOCALES:
        assert actual[locale]['overall'] == reference[locale]['overall']


def test_repeated_short_action_is_allowed_but_duplicate_whole_reading_is_rejected(tmp_path):
    resources = _copy_resources(tmp_path)
    path = resources / 'copy.v2.json'
    asset = json.loads(path.read_text())
    variants = asset['overall']['overall.d50.general']['variants']
    variants['v02']['do'] = variants['v01']['do']
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)
    load_catalog(resources)
    for field in ('headline', 'flow'):
        variants['v02'][field] = variants['v01'][field]
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError, match='readings must be unique'):
        load_catalog(resources)


@pytest.mark.parametrize("text", [
    "마음이 가벼워지겠어", "편하게 다가갈 수 있는 흐름이야", "좋은 가능성이 보여",
    "기운이 모여", "흐름이야.",
])
def test_retired_category_tone_is_rejected(tmp_path, text):
    resources = _copy_resources(tmp_path)
    path = resources / "copy.v2.json"
    asset = json.loads(path.read_text())
    asset["categories"]["category.love.d00.general"]["variants"]["v01"]["text"][0] = text
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError, match="retired fortune"):
        load_catalog(resources)


def test_repeated_category_suggestion_is_allowed_but_same_two_lines_are_not(tmp_path):
    resources = _copy_resources(tmp_path)
    path = resources / "copy.v2.json"
    asset = json.loads(path.read_text())
    variants = asset["categories"]["category.love.d00.general"]["variants"]
    variants["v02"]["text"][1] = variants["v01"]["text"][1]
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)
    load_catalog(resources)

    variants["v02"]["text"][0] = variants["v02"]["text"][1]
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError, match="two distinct text segments"):
        load_catalog(resources)


@pytest.mark.parametrize("locale,filename", [
    ("ko", "copy.v2.json"), ("en", "copy.v2.en.json"), ("ja", "copy.v2.ja.json"),
])
def test_locale_revision_cannot_be_replaced_by_another_release(tmp_path, locale, filename):
    resources = _copy_resources(tmp_path)
    path = resources / filename
    asset = json.loads(path.read_text())
    asset["copy_version"] = COPY_VERSIONS["en" if locale == "ko" else "ko"]
    _write_json(path, asset)
    _refresh_manifest_hash(resources, filename)
    with pytest.raises(FortuneCatalogError, match="schema or version"):
        load_catalog(resources)
