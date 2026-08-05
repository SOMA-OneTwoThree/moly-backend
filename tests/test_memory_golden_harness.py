"""golden 하네스 — 결과를 보고 통과시키는 길을 막는다(16.5절).

"결과를 본 뒤 threshold나 case를 삭제해 통과시키지 않는다"가 문서의 요구다.
그걸 사람 규율이 아니라 코드로 강제하는지 본다.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PATH = _ROOT / "scripts" / "eval_memory_golden.py"
# 스크립트 본문에 __file__이 필요하므로 넣어 준다(모듈로 import되지 않는 CLI라).
_ns: dict = {"__file__": str(_PATH)}
exec(  # noqa: S102
    compile(_PATH.read_text().split("_env, _rest = split_env_arg")[0], str(_PATH), "exec"), _ns
)
assert importlib.util.find_spec("yaml")

FIXTURE = _ROOT / "evals" / "memory_golden_v1.yaml"


def _data():
    return yaml.safe_load(FIXTURE.read_text())


def test_fixture_declares_all_gate_thresholds():
    """16.5절이 요구하는 5개 지표가 fixture에 박혀 있어야 한다."""
    t = _data()["thresholds"]
    assert t["recall_at_5"] >= 0.90
    assert t["precision_at_5"] >= 0.90
    assert t["irrelevant_injection_rate"] <= 0.02
    assert t["routing_accuracy"] >= 0.98
    assert t["greeting_skip_rate"] >= 0.95


def test_required_categories_cover_the_spec():
    cats = set(_data()["required_categories"])
    for needed in (
        "explicit_recall", "implicit_recall", "greeting", "contradiction",
        "date_query", "exact_quote", "diary", "current_state", "locale_change",
    ):
        assert needed in cats


def test_minimum_case_count_is_two_hundred():
    assert _data()["min_cases"] == 200


def test_coverage_gate_blocks_when_cases_are_short():
    """적은 case로 높은 점수를 내는 길을 막는다 — 지표 계산 자체를 안 한다."""
    data = {**_data(), "cases": _data()["cases"][:3]}
    problems = _ns["check_coverage"](data)
    assert any("최소" in p for p in problems)


def test_coverage_gate_blocks_missing_category():
    data = {**_data(), "cases": [{"id": "x", "category": "greeting"}], "min_cases": 1}
    problems = _ns["check_coverage"](data)
    assert any("빠진 category" in p for p in problems)


def test_coverage_gate_blocks_duplicate_ids():
    dup = [{"id": "a", "category": c} for c in _data()["required_categories"]]
    dup.append({"id": "a", "category": "greeting"})
    problems = _ns["check_coverage"]({**_data(), "cases": dup, "min_cases": 1})
    assert any("중복" in p for p in problems)


def test_fixture_hash_changes_when_content_changes(tmp_path):
    """shadow 전 고정한 hash와 달라지면 사후 수정이 드러난다."""
    a = tmp_path / "a.yaml"
    a.write_text("version: 1\n")
    h1 = _ns["fixture_hash"](a)
    a.write_text("version: 2\n")
    assert _ns["fixture_hash"](a) != h1


def test_thresholds_are_not_overridable_from_cli():
    """CLI로 기준을 낮출 수 있으면 게이트가 아니다."""
    src = _PATH.read_text()
    assert "--recall" not in src and "--threshold" not in src
    assert "add_argument" not in src


def test_current_fixture_intentionally_fails_gate():
    """지금은 시드만 있다 — 통과한다고 보고되면 안 된다."""
    assert _ns["check_coverage"](_data()), "case 미달인데 게이트가 통과로 보고한다"


def test_greeting_cases_expect_provider_skip():
    """단순 인사에 provider를 부르면 비용과 오주입이 늘어난다."""
    greetings = [c for c in _data()["cases"] if c.get("category") == "greeting"]
    assert greetings
    for c in greetings:
        assert c.get("expect_provider_skip") is True
        assert c.get("expect_recall") is False


def test_date_and_quote_cases_route_to_timeline_not_mem0():
    """날짜·정확 문구는 timeline이 정본이다 — 의미 검색으로 답하면 안 된다."""
    for cat in ("date_query", "exact_quote"):
        cases = [c for c in _data()["cases"] if c.get("category") == cat]
        assert cases
        for c in cases:
            assert c.get("expect_route") == "recall_timeline"
            assert c.get("expect_recall") is False
