from __future__ import annotations

from pathlib import Path

import yaml


_CORPUS = Path(__file__).parent / "golden" / "conversational_recall" / "scenarios.yaml"
_RECALL = {"diary", "memory", "routine", "focus"}


def _load() -> dict:
    value = yaml.safe_load(_CORPUS.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_conversational_recall_corpus_has_unique_well_formed_cases() -> None:
    corpus = _load()
    assert corpus["version"] == 1
    cases = corpus["cases"]
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))
    assert len(cases) >= 15

    for case in cases:
        assert case["language"] in {"ko", "en", "ja"}
        assert case["turns"]
        for turn in case["turns"]:
            assert turn["user"].strip()
            assert set(turn.get("expect_recall", ())) <= _RECALL
            assert turn["expect_response"]


def test_conversational_recall_corpus_covers_every_required_domain_and_language() -> None:
    corpus = _load()
    cases = corpus["cases"]
    domains = {case["domain"] for case in cases}
    assert set(corpus["required_domains"]) <= domains
    assert {case["language"] for case in cases} == {"ko", "en", "ja"}


def test_corpus_contains_positive_negative_failure_and_privacy_controls() -> None:
    cases = _load()["cases"]
    assert any(turn.get("expect_recall") == [] for c in cases for turn in c["turns"])
    assert any(c.get("injected_failures") for c in cases)
    assert any(c["domain"] == "suppression" for c in cases)
    assert any(c["domain"] == "prompt_injection" for c in cases)
    assert any("diary-reference-v1" in c.get("capabilities", []) for c in cases)
