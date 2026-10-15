"""Offline generator review gates; run only in the authorized test environment."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

from scripts import build_fortune_localized_copy as builder
from scripts import check_fortune_tarot_editorial as checker


@pytest.mark.parametrize("damage", ["incomplete_proof", "stale_artifact"])
def test_ci_audit_rejects_supplemental_review_failure(tmp_path, monkeypatch, damage):
    """Even matching generated assets must not bypass supplemental review gates."""
    directory = tmp_path / "docs/fortune-content/localization"
    directory.mkdir(parents=True)
    result = {"status": "pass", "errors": [], "pending": []}
    if damage == "incomplete_proof":
        result.update(status="incomplete", pending=[{"reason": "repetition_review_incomplete_or_stale"}])
    (directory / "validation.json").write_bytes(builder.encode(result) if damage == "incomplete_proof" else b"{}")
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    monkeypatch.setitem(sys.modules, "scripts.build_fortune_localized_copy", SimpleNamespace(
        VERSION=builder.VERSION, build_outputs=lambda root: {}, encode=builder.encode,
        read_json=lambda path: {"readings": {}},
    ))
    monkeypatch.setattr(checker.importlib.util, "spec_from_file_location", lambda *args: SimpleNamespace(
        loader=SimpleNamespace(exec_module=lambda module: None),
    ))
    monkeypatch.setattr(checker.importlib.util, "module_from_spec", lambda spec: SimpleNamespace(audit=lambda: result))
    monkeypatch.setattr(checker, "audit_readings", lambda *args, **kwargs: {"failures": []})
    failures = checker.audit_localized()["failures"]
    expected = "incomplete or stale localization review" if damage == "incomplete_proof" else "stale validation artifact"
    assert any(item["reason"] == expected for item in failures)


@pytest.fixture
def reviewed_tree(tmp_path):
    root = Path(__file__).resolve().parents[1]
    for relative in ("app/resources/fortune", "docs/fortune-content/ko-rewrite",
                     "docs/fortune-content/localization", "docs/fortune-content/ko-plain-voice"):
        shutil.copytree(root / relative, tmp_path / relative)
    return tmp_path


@pytest.mark.parametrize("damage", ["missing_locale", "stale_review", "stale_source", "author_incomplete", "weak_score", "open_finding"])
def test_unreviewed_generation_fails_before_asset_writes(reviewed_tree, damage):
    root = reviewed_tree
    directory = root / "docs/fortune-content/localization"
    resources = root / "app/resources/fortune"
    before = {path.name: path.read_bytes() for path in resources.iterdir() if path.is_file()}
    review_path = directory / "review.json"
    review = builder.read_json(review_path)
    key = next(iter(review["locales"]["en"]))
    if damage == "missing_locale":
        (directory / "ja/money.json").unlink()
    elif damage == "author_incomplete":
        path = directory / "en/author-review.json"
        author = builder.read_json(path)
        author["status"] = "in_progress"
        path.write_text(json.dumps(author))
    elif damage == "stale_review":
        review["locales"]["en"][key]["read_bundle_sha256"] = "0" * 64
    elif damage == "stale_source":
        review["locales"]["en"][key]["source_ko_sha256"] = "0" * 64
    elif damage == "weak_score":
        review["locales"]["en"][key]["scores"]["meaning_parity"] = 3
    else:
        review.setdefault("findings", []).append({"status": "open", "key": key})
    review_path.write_text(json.dumps(review))
    with pytest.raises((ValueError, OSError)):
        builder.build_outputs(root)
    assert before == {path.name: path.read_bytes() for path in resources.iterdir() if path.is_file()}


def test_generation_preserves_first_visit_cards_rules_legacy_copy_and_colors(reviewed_tree):
    resource = reviewed_tree / "app/resources/fortune"
    original = builder.read_json(resource / "first-visit.v1.json")
    outputs = builder.build_outputs(reviewed_tree)
    generated = json.loads(outputs[resource / "first-visit.v1.json"])
    assert [(e["id"], e["cards"]) for e in generated["sets"]] == [(e["id"], e["cards"]) for e in original["sets"]]
    assert resource / "rules.v2.json" not in outputs
    for locale, filename in builder.FILES.items():
        asset = json.loads(outputs[resource / filename])
        old = builder.read_json(resource / filename)
        assert asset["colors"] == old["colors"]
        assert resource / filename.replace("v3", "v2") not in outputs
        for entry in generated["sets"]:
            for axis, card in entry["cards"].items():
                key = f"{axis}.{card['card_id']}.{card['orientation']}"
                selected = entry["copy_by_locale"][locale]
                actual = selected["overall"] if axis == "overall" else selected["categories"][axis]
                assert actual == asset["readings"][key]


def test_duplicate_source_keys_are_rejected(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"same":1,"same":2}')
    with pytest.raises(ValueError, match="duplicate"):
        builder.read_json(path)


@pytest.mark.parametrize("damage", ["changed_copy", "missing_key", "stale_author", "failed_independent", "stale_artifact"])
def test_korean_override_cannot_bypass_its_own_review(reviewed_tree, damage):
    directory = reviewed_tree / "docs/fortune-content/ko-plain-voice"
    path = directory / "overall.json"
    values = builder.read_json(path)
    key = next(iter(values))
    if damage == "changed_copy":
        values[key]["headline"] = "검수하지 않은 문구."
        path.write_bytes(builder.encode(values))
    elif damage == "missing_key":
        del values[key]
        path.write_bytes(builder.encode(values))
    elif damage in {"stale_author", "failed_independent"}:
        kind = "author" if damage == "stale_author" else "independent"
        path = directory / f"overall.{kind}-review.json"
        proof = builder.read_json(path)
        if damage == "stale_author":
            proof["readings"][key]["read_bundle_sha256"] = "0" * 64
        else:
            proof["readings"][key]["status"] = "revise"
        path.write_bytes(builder.encode(proof))
    else:
        (directory / "generated/overall.md").write_text("Stale review artifact.\n")
    resources = reviewed_tree / "app/resources/fortune"
    before = {p.name: p.read_bytes() for p in resources.iterdir() if p.is_file()}
    with pytest.raises(ValueError, match="Korean plain-voice"):
        builder.build_outputs(reviewed_tree)
    assert before == {p.name: p.read_bytes() for p in resources.iterdir() if p.is_file()}


def test_korean_override_preserves_foreign_copy_and_original_review_sources(reviewed_tree):
    docs = reviewed_tree / "docs/fortune-content"
    protected = [p for folder in ("ko-rewrite", "localization")
                 for p in (docs / folder).rglob("*.json")]
    before = {p: p.read_bytes() for p in protected}
    outputs = builder.build_outputs(reviewed_tree)
    for locale, filename in builder.FILES.items():
        generated = json.loads(outputs[reviewed_tree / "app/resources/fortune" / filename])
        source = docs / ("ko-plain-voice" if locale == "ko" else f"localization/{locale}")
        for axis in builder.AXES:
            for key, bundle in builder.read_json(source / f"{axis}.json").items():
                assert generated["readings"][key] == builder.to_wire(key, bundle)
    assert before == {p: p.read_bytes() for p in protected}


def test_love_scenarios_are_not_joined_to_fit_the_legacy_contract():
    bundle = {"paragraphs": ["Single.", "In a relationship.", "After a breakup."]}
    builder.validate_bundle("love.major.sun.upright", bundle, "en")
    broken = deepcopy(bundle)
    broken["paragraphs"] = ["Single. In a relationship.", "After a breakup."]
    with pytest.raises(ValueError, match="paragraph count"):
        builder.validate_bundle("love.major.sun.upright", broken, "en")
