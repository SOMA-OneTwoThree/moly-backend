"""Offline evaluation guards: no credentials, provider, network or database."""
from copy import deepcopy
from decimal import Decimal

import pytest

from scripts import eval_conversation_quality as evaluation


def test_stage_budgets_cover_the_frozen_split_without_reusing_pilot_in_main():
    cases, fixture_hash = evaluation.load_cases()
    stages = {name: evaluation.make_plan(cases, fixture_hash, name)
              for name in ("pilot", "main", "holdout", "repeat", "fixed", "autonomy", "smoke")}
    assert {k: v["generation_turns"] for k, v in stages.items()} == {
        "pilot": 48, "main": 144, "holdout": 96, "repeat": 96, "fixed": 72, "autonomy": 18, "smoke": 6,
    }
    assert sum(p["generation_turns"] for p in stages.values()) == 480
    pilot = {p["case_id"] for p in stages["pilot"]["pairs"]}
    main = {p["case_id"] for p in stages["main"]["pairs"]}
    assert not pilot & main
    assert all(not p["execution_authorized"] for p in stages.values())


def test_synthetic_state_keeps_unknown_and_locale_changes_on_following_turns():
    cases, _ = evaluation.load_cases()
    assert evaluation.state_at(cases["ko_h04"], 3)["mood"] == "unknown"
    assert evaluation.state_at(cases["ko_d08"], 2)["locale"] == "ko"
    assert evaluation.state_at(cases["ko_d08"], 3)["locale"] == "en"
    assert evaluation.state_at(cases["ko_h03"], 2)["now"].startswith("2026-11-04T00:01")


def test_schema_rejects_unreviewed_context_and_identifiers():
    cases, _ = evaluation.load_cases()
    case = deepcopy(cases["ko_d01"])
    case["setup"]["user_id"] = "unapproved"
    with pytest.raises(ValueError, match="setup"):
        evaluation.validate_case(case)
    case = deepcopy(cases["ko_d01"])
    case["setup"]["memories"] = ["unreviewed"]
    with pytest.raises(ValueError, match="memory"):
        evaluation.validate_case(case)


def test_budget_unknown_cost_and_exhaustion_fail_before_mutation():
    budget = evaluation.Budget()
    with pytest.raises(ValueError, match="known positive"):
        budget.reserve(dollars=None, attempts=1, input_tokens=10, output_tokens=10)
    assert budget.generation_turns == 0
    budget.reserve(dollars=Decimal("14.99"), attempts=1, input_tokens=10, output_tokens=10)
    with pytest.raises(ValueError, match="exhausted"):
        budget.reserve(dollars=Decimal("0.02"), attempts=1, input_tokens=10, output_tokens=10)
    assert budget.dollars == Decimal("14.99") and budget.generation_turns == 1


def test_approved_generation_and_attempt_limits_preserve_money_and_token_caps():
    budget = evaluation.Budget()
    budget.reserve(dollars=Decimal("1"), attempts=1500, input_tokens=10, output_tokens=10)
    budget.reserve(dollars=Decimal("1"), attempts=100, input_tokens=10, output_tokens=10)
    with pytest.raises(ValueError, match="exhausted"):
        budget.reserve(dollars=Decimal("1"), attempts=1, input_tokens=10, output_tokens=10)
    assert budget.attempts == 1600 and budget.dollars == Decimal("2")
    assert evaluation.BUDGET_LIMITS == {"dollars": "15", "generation_turns": 492,
                                      "attempts": 1600, "input_tokens": 50_000_000,
                                      "output_tokens": 600_000}


def capture_fixture():
    cases, fixture_hash = evaluation.load_cases()
    capture = {
        "baseline_sha": evaluation.BASELINE_SHA, "fixture_hash": fixture_hash,
        "candidate_sha": "synthetic-candidate-revision", "model_config_hash": "fixed-settings",
        "scope": "source_prompt",
        "records": [{"case_id": "ko_d01", "variant": variant, "outputs": ["합성 응답"] * 4,
                     "model_snapshot": "synthetic-snapshot", "deployment_sha": "hidden-revision",
                     "cost_upper_usd": "0.01"} for variant in ("baseline", "candidate")],
    }
    return cases, fixture_hash, capture


def test_blind_packet_hides_arm_and_cost_but_rejects_unpaired_or_different_model():
    cases, fixture_hash, capture = capture_fixture()
    packet, secret = evaluation.blind_pack(capture, cases, fixture_hash, seed=9)
    wire = str(packet)
    assert "candidate" not in wire and "baseline" not in wire
    assert "cost_upper_usd" not in wire and "hidden-revision" not in wire
    assert secret["packet_hash"] == evaluation.digest(packet)
    capture["records"].pop()
    with pytest.raises(ValueError, match="unpaired"):
        evaluation.blind_pack(capture, cases, fixture_hash, seed=9)
    cases, fixture_hash, capture = capture_fixture()
    capture["records"][1]["model_snapshot"] = "different-snapshot"
    with pytest.raises(ValueError, match="snapshots"):
        evaluation.blind_pack(capture, cases, fixture_hash, seed=9)


def test_fixed_probe_requires_the_exact_frozen_synthetic_history():
    cases, fixture_hash, capture = capture_fixture()
    for row in capture["records"]:
        row.update(mode="fixed_history", outputs=["응."], history_hash="same-but-wrong")
    with pytest.raises(ValueError, match="synthetic prefix"):
        evaluation.blind_pack(capture, cases, fixture_hash, seed=9)


def test_fluid_writing_cannot_hide_a_functional_hard_failure():
    cases, fixture_hash, capture = capture_fixture()
    packet, secret = evaluation.blind_pack(capture, cases, fixture_hash, seed=9)
    pair_id = packet["pairs"][0]["pair_id"]
    candidate_side = next(k for k in ("A", "B") if secret["pairs"][pair_id][k] == "candidate")
    review = {
        "pair_id": pair_id, "winner": candidate_side, "rationale": "Synthetic review rationale.",
        "scores": {s: {d: 2 for d in evaluation.DIMENSIONS} for s in ("A", "B")},
        "hard_failures": {s: [] for s in ("A", "B")},
    }
    review["hard_failures"][candidate_side] = [{
        "gate": "unsolicited_mood", "evidence": "Turn 1 unexpectedly named the app selection.",
    }]
    summary = evaluation.summarize_reviews(packet, secret, [review])
    assert not summary["within_packet_gate"] and not summary["release_approved"]
    with pytest.raises(ValueError, match="missing reviews"):
        evaluation.summarize_reviews(packet, secret, [])


def test_smoke_cannot_be_presented_as_source_paired_evidence():
    cases, fixture_hash, capture = capture_fixture()
    capture["scope"] = "dev_smoke"
    with pytest.raises(ValueError, match="smoke"):
        evaluation.blind_pack(capture, cases, fixture_hash, seed=9)


def test_early_stop_cannot_be_repackaged_as_a_clean_completed_pair():
    cases, fixture_hash, capture = capture_fixture()
    capture.update(completed=False, stop_reason="budget_guard")
    with pytest.raises(ValueError, match="incomplete"):
        evaluation.blind_pack(capture, cases, fixture_hash, seed=9)
