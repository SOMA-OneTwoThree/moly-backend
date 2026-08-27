from __future__ import annotations

import hashlib
import json
from difflib import SequenceMatcher
from pathlib import Path
import shutil

import pytest

from app.services.fortune_catalog import (
    COPY_VERSION,
    EXPECTED_CATEGORY_KEYS,
    EXPECTED_OVERALL_KEYS,
    FortuneCatalogError,
    load_catalog,
    render_all,
)

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


def test_seed_catalog_has_complete_safe_coverage():
    catalog = load_catalog()
    assert COPY_VERSION == "fortune-copy.v2-seed.3"
    assert set(catalog.overall) == set(EXPECTED_OVERALL_KEYS)
    assert set(catalog.categories) == set(EXPECTED_CATEGORY_KEYS)
    assert len(catalog.overall) == 80
    assert len(catalog.categories) == 40
    assert len(catalog.colors) == 12
    assert len(catalog.manifest_hash) == 64


def test_all_seed_expressions_are_unique_and_not_near_duplicates():
    asset = json.loads((RESOURCE_DIR / "copy.v2.json").read_text(encoding="utf-8"))
    expressions = []
    for bundle in asset["overall"].values():
        expressions.extend((bundle["headline"], *bundle["flow"], bundle["do"], bundle["pause"]))
    for block in asset["categories"].values():
        expressions.extend(block["text"])

    assert len(expressions) == 560
    assert len(expressions) == len(set(expressions))
    sentences = [value for value in expressions if len(value) >= 12]
    near_duplicates = [
        (left, right)
        for index, left in enumerate(sentences)
        for right in sentences[index + 1:]
        if SequenceMatcher(None, left, right).ratio() >= 0.72
    ]
    assert near_duplicates == []


def test_no_response_can_repeat_the_reviewed_canned_terms():
    asset = json.loads((RESOURCE_DIR / "copy.v2.json").read_text(encoding="utf-8"))
    watched = (
        "무난하게", "차분히", "편하게", "가볍게", "자연스럽게",
        "분명하게", "꼼꼼히", "천천히", "서두르", "좋은 날이야", "좋아.",
    )
    overall_blocks = [
        "\n".join((bundle["headline"], *bundle["flow"], bundle["do"], bundle["pause"]))
        for bundle in asset["overall"].values()
    ]
    category_blocks = {
        category: [
            "\n".join(block["text"])
            for route, block in asset["categories"].items()
            if route.startswith(f"category.{category}.")
        ]
        for category in ("love", "money", "work", "energy")
    }
    for term in watched:
        maximum_in_one_response = max(block.count(term) for block in overall_blocks) + sum(
            max(block.count(term) for block in blocks)
            for blocks in category_blocks.values()
        )
        assert maximum_in_one_response <= 1, term


def test_manifest_hashes_rules_and_copy_together():
    catalog = load_catalog()
    manifest = json.loads((RESOURCE_DIR / "manifest.v2.json").read_text(encoding="utf-8"))
    for filename in ("rules.v2.json", "copy.v2.json"):
        actual = hashlib.sha256((RESOURCE_DIR / filename).read_bytes()).hexdigest()
        assert manifest["assets"][filename]["sha256"] == actual
        assert catalog.asset_hashes[filename] == actual


def test_render_returns_atomic_korean_bundle_and_four_two_line_categories():
    rendered = render_all(_semantic())
    assert set(rendered) == {"ko"}
    result = rendered["ko"]
    assert set(result["overall"]) == {"headline", "flow", "do", "pause"}
    assert len(result["overall"]["flow"]) == 3
    assert set(result["categories"]) == {"love", "money", "work", "energy"}
    assert all(len(value["text"]) == 2 for value in result["categories"].values())
    assert result["lucky_color"] == {"key": "blue", "name": "파랑", "hex": "#1E88E5"}


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
        render_all(semantic)


def test_missing_route_is_rejected_even_with_a_valid_hash(tmp_path: Path):
    resources = _copy_resources(tmp_path)
    path = resources / "copy.v2.json"
    asset = json.loads(path.read_text(encoding="utf-8"))
    asset["overall"].pop("overall.d00.start.default")
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError, match="overall routes keys mismatch"):
        load_catalog(resources)


@pytest.mark.parametrize("bad", ["힘을 써", "오늘은 신호에 가까워", "좋을 수도 나쁠 수도 있어"])
def test_forbidden_korean_copy_is_rejected(tmp_path: Path, bad: str):
    resources = _copy_resources(tmp_path)
    path = resources / "copy.v2.json"
    asset = json.loads(path.read_text(encoding="utf-8"))
    asset["overall"]["overall.d00.start.default"]["headline"] = bad
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError, match="forbidden fortune wording"):
        load_catalog(resources)


@pytest.mark.parametrize(
    "bad",
    ["앞서가기보다", "눈에 띄는 진전을 만들 수 있어", "무난하게 이어지는 날이야"],
)
def test_awkward_korean_copy_is_rejected(tmp_path: Path, bad: str):
    resources = _copy_resources(tmp_path)
    path = resources / "copy.v2.json"
    asset = json.loads(path.read_text(encoding="utf-8"))
    asset["overall"]["overall.d00.start.default"]["headline"] = bad
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError, match="awkward Korean fortune wording"):
        load_catalog(resources)


def test_duplicate_expression_is_rejected_across_routes(tmp_path: Path):
    resources = _copy_resources(tmp_path)
    path = resources / "copy.v2.json"
    asset = json.loads(path.read_text(encoding="utf-8"))
    asset["categories"]["category.money.d00.general"]["text"][0] = (
        asset["categories"]["category.love.d00.general"]["text"][0]
    )
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError, match="expressions must be unique"):
        load_catalog(resources)


def test_overall_category_word_and_bad_flow_length_are_rejected(tmp_path: Path):
    resources = _copy_resources(tmp_path)
    path = resources / "copy.v2.json"
    asset = json.loads(path.read_text(encoding="utf-8"))
    asset["overall"]["overall.d00.start.default"]["headline"] = "오늘은 지출을 조심하는 게 좋아."
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError, match="category-specific"):
        load_catalog(resources)

    resources = _copy_resources(tmp_path / "flow")
    path = resources / "copy.v2.json"
    asset = json.loads(path.read_text(encoding="utf-8"))
    asset["overall"]["overall.d00.start.default"]["flow"].pop()
    _write_json(path, asset)
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError, match="exactly three"):
        load_catalog(resources)


def test_duplicate_json_key_is_rejected_before_schema_validation(tmp_path: Path):
    resources = _copy_resources(tmp_path)
    path = resources / "copy.v2.json"
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw.replace("{\n", '{\n  "schema": "fortune-copy-v2",\n', 1), encoding="utf-8")
    _refresh_manifest_hash(resources, path.name)
    with pytest.raises(FortuneCatalogError, match="duplicate JSON key: schema"):
        load_catalog(resources)
