"""Offline planning and blind review for newly authored synthetic conversations.

This module performs no network calls, reads no credentials and touches no DB.
An approved runner must capture responses through the existing development API.
Injected context, actual chat persistence and retrieval are separate evidence scopes.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import random

ROOT = Path(__file__).resolve().parents[1]
BASELINE_SHA = "6fb3ea72f9703456301d73dc66e7098ff792d3d2"
FIXTURES = (
    ROOT / "evals/conversation_quality_v1.json",
    ROOT / "evals/conversation_quality_holdout_v1.json",
    ROOT / "evals/conversation_quality_autonomy_v1.json",
)
MOOD_PROBE_FIXTURE = ROOT / "evals/conversation_quality_mood_probe_v1.json"
LOCALES = {"ko", "ja", "en"}
MOODS = {"annoyed", "tired", "neutral", "content", "excited", "unknown"}
DIMENSIONS = (
    "responsiveness", "progress", "non_repetition", "warmth_distance", "naturalness",
)
HARD_GATES = {
    "invented_fact", "correction_lost", "source_contamination", "stale_state",
    "unsolicited_mood", "unavailable_record_invented", "answered_question_repeated",
    "question_boundary_ignored", "relationship_escalation", "wrong_locale",
    "privacy_or_tool_boundary", "safety_regression", "autonomy_override",
}
SCOPES = {"source_prompt", "dev_smoke", "actual_chat", "registry_retrieval"}
BUDGET_LIMITS = {"dollars": "15", "generation_turns": 492, "attempts": 1600,
                 "input_tokens": 50_000_000, "output_tokens": 600_000}


def digest(value: object) -> str:
    wire = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(wire.encode()).hexdigest()


def load_cases(paths: tuple[Path, ...] = FIXTURES, *, mood_probe: bool = False) -> tuple[dict, str]:
    manifests = [json.loads(p.read_text()) for p in paths]
    cases: dict[str, dict] = {}
    for manifest in manifests:
        if manifest.get("baseline_sha") != BASELINE_SHA:
            raise ValueError("baseline SHA mismatch")
        if manifest.get("data_origin") != "new_synthetic_only":
            raise ValueError("only newly authored synthetic fixtures are allowed")
        split = manifest.get("split")
        if split not in ({"mood_probe"} if mood_probe else {"dev", "holdout", "autonomy"}):
            raise ValueError("invalid split")
        for raw in manifest["cases"]:
            case = {**raw, "split": split}
            validate_case(case)
            if case["id"] in cases:
                raise ValueError("duplicate case id")
            cases[case["id"]] = case
    counts = Counter((c["split"], c["locale"]) for c in cases.values())
    sizes = (("mood_probe", 2),) if mood_probe else (("dev", 8), ("holdout", 4), ("autonomy", 3))
    expected = {(s, lang): n for s, n in sizes for lang in LOCALES}
    if counts != expected:
        raise ValueError("fixture bundle coverage mismatch")
    return cases, digest(manifests)


def load_stage_cases(stage: str) -> tuple[dict, str]:
    return (load_cases((MOOD_PROBE_FIXTURE,), mood_probe=True) if stage == "mood_probe" else load_cases())


def validate_case(case: dict) -> None:
    if set(case) != {"id", "locale", "family", "source", "setup", "turns", "checks", "split"}:
        raise ValueError("unexpected case fields")
    if case["locale"] not in LOCALES or case["source"] != "new_synthetic":
        raise ValueError("invalid locale/source")
    setup = case["setup"]
    if set(setup) != {"timezone", "at", "mood", "memories"} or setup["mood"] not in MOODS:
        raise ValueError("invalid synthetic setup")
    if datetime.fromisoformat(setup["at"]).tzinfo is None:
        raise ValueError("fixture clock requires an explicit offset")
    # New memory scenarios need a separately reviewed bounded schema before enabling.
    if setup["memories"] != []:
        raise ValueError("unreviewed memory fixture")
    if len(case["turns"]) != (1 if case["split"] == "mood_probe" else 4) or not case["checks"]:
        raise ValueError("the bundle's fixed turn count and explicit checks are required")
    previous = -1
    for turn in case["turns"]:
        if not {"text", "minutes_after_start"} <= set(turn) <= {
            "text", "minutes_after_start", "locale", "mood",
        }:
            raise ValueError("unexpected turn fields")
        if not isinstance(turn["text"], str) or not 1 <= len(turn["text"]) <= 2000:
            raise ValueError("invalid synthetic text")
        minute = turn["minutes_after_start"]
        if type(minute) is not int or minute < previous:
            raise ValueError("turn times must be monotonic integers")
        previous = minute
        if turn.get("locale", case["locale"]) not in LOCALES:
            raise ValueError("unsupported locale override")
        if turn.get("mood", setup["mood"]) not in MOODS:
            raise ValueError("unsupported mood override")


def state_at(case: dict, turn_index: int) -> dict:
    if not 0 <= turn_index < len(case["turns"]):
        raise ValueError("turn index out of bounds")
    locale, mood = case["locale"], case["setup"]["mood"]
    for turn in case["turns"][:turn_index + 1]:
        locale, mood = turn.get("locale", locale), turn.get("mood", mood)
    at = datetime.fromisoformat(case["setup"]["at"]) + timedelta(
        minutes=case["turns"][turn_index]["minutes_after_start"]
    )
    previous = None if turn_index == 0 else datetime.fromisoformat(case["setup"]["at"]) + timedelta(
        minutes=case["turns"][turn_index - 1]["minutes_after_start"]
    )
    return {"locale": locale, "mood": mood, "now": at.isoformat(),
            "previous": previous.isoformat() if previous else None,
            "timezone": case["setup"]["timezone"]}


def make_plan(cases: dict, fixture_hash: str, stage: str) -> dict:
    if stage not in {"pilot", "main", "holdout", "repeat", "fixed", "autonomy", "mood_probe", "smoke"}:
        raise ValueError("unknown evaluation stage")
    pilot = {"ordinary", "mood"}
    selected = [c for c in cases.values() if (
        (stage == "pilot" and c["split"] == "dev" and c["family"] in pilot)
        or (stage == "main" and c["split"] == "dev" and c["family"] not in pilot)
        or (stage == "holdout" and c["split"] == "holdout")
        or (stage == "repeat" and c["split"] == "dev" and c["family"] in {
            "answered_question", "correction", "elapsed", "mood"})
        or (stage == "fixed" and c["split"] in {"dev", "holdout"})
        or (stage == "autonomy" and c["split"] == "autonomy")
        or (stage == "mood_probe" and c["split"] == "mood_probe")
        or (stage == "smoke" and c["split"] == "dev" and c["family"] in pilot)
    )]
    variants = ("deployed",) if stage == "smoke" else ("baseline", "candidate")
    plans = [{"case_id": c["id"], "variant": variant,
              "turns": [3] if stage in {"fixed", "autonomy"} else [0] if stage in {"smoke", "mood_probe"} else list(range(4))}
             for c in selected for variant in variants]
    return {"version": 1, "baseline_sha": BASELINE_SHA, "fixture_hash": fixture_hash,
            "stage": stage, "scope": "dev_smoke" if stage == "smoke" else "source_prompt",
            "mode": "fixed_history" if stage in {"fixed", "autonomy", "mood_probe"} else "rollout",
            "generation_turns": sum(len(p["turns"]) for p in plans),
            "execution_authorized": False, "pairs": plans}


def fixed_history(case: dict) -> list[dict[str, str]]:
    """Frozen synthetic acknowledgements isolate the final response, not dialogue flow."""
    acknowledgements = {
        "ko": ("응. 듣고 있어.", "아 그렇구나.", "응."),
        "ja": ("うん。聞いてるよ。", "そうだったんだね。", "うん。"),
        "en": ("I'm listening.", "Oh I see.", "Okay."),
    }[case["locale"]]
    history = []
    for index, turn in enumerate(case["turns"]):
        history.append({"role": "user", "content": turn["text"]})
        if index < len(case["turns"]) - 1:
            history.append({"role": "assistant", "content": acknowledgements[index]})
    return history


@dataclass
class Budget:
    """Conservative pre-call reservations; unknown outcome never refunds money.

    The runner must persist this state after EVERY reservation before external I/O.
    Upper cost includes all possible retries, uncached input and full output limits.
    Separate runs/stages share one persisted budget, never a fresh $15 each.
    """
    dollars: Decimal = Decimal("0")
    generation_turns: int = 0
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def reserve(self, *, dollars: Decimal | None, attempts: int,
                input_tokens: int, output_tokens: int) -> None:
        if dollars is None or not dollars.is_finite() or dollars <= 0:
            raise ValueError("a known positive price-based upper bound is required")
        if any(type(n) is not int or n <= 0 for n in (attempts, input_tokens, output_tokens)):
            raise ValueError("positive integer reservation is required")
        proposal = (self.dollars + dollars, self.generation_turns + 1,
                    self.attempts + attempts, self.input_tokens + input_tokens,
                    self.output_tokens + output_tokens)
        if any(n > cap for n, cap in zip(proposal, (Decimal(BUDGET_LIMITS["dollars"]),
                   BUDGET_LIMITS["generation_turns"], BUDGET_LIMITS["attempts"],
                   BUDGET_LIMITS["input_tokens"], BUDGET_LIMITS["output_tokens"]))):
            raise ValueError("evaluation budget exhausted")
        (self.dollars, self.generation_turns, self.attempts,
         self.input_tokens, self.output_tokens) = proposal


def blind_pack(capture: dict, cases: dict, fixture_hash: str, seed: int) -> tuple[dict, dict]:
    if capture.get("completed") is False or capture.get("failures") or capture.get("stop_reason"):
        raise ValueError("incomplete or failed stage cannot become a clean blind packet")
    if capture.get("baseline_sha") != BASELINE_SHA or capture.get("fixture_hash") != fixture_hash:
        raise ValueError("capture source/fixture mismatch")
    if capture.get("scope") != "source_prompt":
        raise ValueError("smoke and real pipeline evidence cannot enter prompt paired scores")
    if not capture.get("candidate_sha") or not capture.get("model_config_hash"):
        raise ValueError("candidate revision and model configuration are required")
    grouped: dict[tuple, dict] = defaultdict(dict)
    for row in capture["records"]:
        case_id, variant = row["case_id"], row["variant"]
        if case_id not in cases or variant not in {"baseline", "candidate"}:
            raise ValueError("unknown case or variant")
        mode = row.get("mode", "rollout")
        if mode not in {"rollout", "fixed_history"}:
            raise ValueError("invalid capture mode")
        turns = row["outputs"]
        if len(turns) != (4 if mode == "rollout" else 1):
            raise ValueError("incomplete transcript; errors must be reported separately")
        if any(not isinstance(t, str) or not t.strip() or len(t) > 4000 for t in turns):
            raise ValueError("invalid response text")
        if mode == "fixed_history" and not row.get("history_hash"):
            raise ValueError("fixed history must have a shared frozen history hash")
        if mode == "fixed_history" and row["history_hash"] != digest(fixed_history(cases[case_id])):
            raise ValueError("history does not match the newly authored synthetic prefix")
        key = (case_id, mode, row.get("repetition", 0))
        if variant in grouped[key]:
            raise ValueError("duplicate capture")
        grouped[key][variant] = row
    rng = random.Random(seed)
    keys = list(grouped)
    rng.shuffle(keys)
    pairs, mapping = [], {}
    for number, key in enumerate(keys, 1):
        variants = grouped[key]
        if set(variants) != {"baseline", "candidate"}:
            raise ValueError("unpaired capture")
        if variants["baseline"].get("model_snapshot") != variants["candidate"].get("model_snapshot"):
            raise ValueError("paired model snapshots differ")
        if not variants["baseline"].get("model_snapshot"):
            raise ValueError("missing actual model snapshot")
        if key[1] == "fixed_history" and variants["baseline"]["history_hash"] != variants["candidate"]["history_hash"]:
            raise ValueError("fixed histories differ")
        orientation = ["baseline", "candidate"]
        rng.shuffle(orientation)
        pair_id = f"pair-{number:03d}"
        case = cases[key[0]]
        pairs.append({"pair_id": pair_id, "locale": case["locale"], "split": case["split"],
                      "mode": key[1], "users": [t["text"] for t in case["turns"]],
                      "fixed_history": fixed_history(case) if key[1] == "fixed_history" else None,
                      "states": [state_at(case, i) for i in range(len(case["turns"]))], "checks": case["checks"],
                      "A": variants[orientation[0]]["outputs"],
                      "B": variants[orientation[1]]["outputs"]})
        mapping[pair_id] = {"A": orientation[0], "B": orientation[1], "case_id": key[0],
                            "locale": case["locale"], "split": case["split"], "mode": key[1]}
    packet = {"version": 1, "fixture_hash": fixture_hash, "pairs": pairs}
    secret = {"version": 1, "packet_hash": digest(packet), "pairs": mapping}
    return packet, secret


def summarize_reviews(packet: dict, secret: dict, reviews: list[dict]) -> dict:
    if digest(packet) != secret.get("packet_hash"):
        raise ValueError("blind packet changed")
    expected = set(secret["pairs"])
    seen: set[str] = set()
    counts: dict[str, Counter] = defaultdict(Counter)
    quality: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    failures = []
    for review in reviews:
        pair_id = review["pair_id"]
        if pair_id not in expected or pair_id in seen:
            raise ValueError("unknown or duplicated review")
        seen.add(pair_id)
        mapping = secret["pairs"][pair_id]
        group = f'{mapping["split"]}/{mapping["locale"]}/{mapping["mode"]}'
        if review["winner"] not in {"A", "B", "tie"} or not review.get("rationale"):
            raise ValueError("winner and grounded rationale required")
        result = "tie" if review["winner"] == "tie" else mapping[review["winner"]]
        counts[group][result] += 1
        for side in ("A", "B"):
            variant = mapping[side]
            scores = review["scores"][side]
            if set(scores) != set(DIMENSIONS) or any(type(v) is not int or v not in (0, 1, 2) for v in scores.values()):
                raise ValueError("all five dimensions need independent 0/1/2 ratings")
            for dimension, score in scores.items():
                quality[group][f"{variant}/{dimension}"].append(score)
            for failure in review["hard_failures"][side]:
                if failure.get("gate") not in HARD_GATES or not failure.get("evidence"):
                    raise ValueError("hard gate needs a known type and turn-specific evidence")
                failures.append({"pair_id": pair_id, "variant": variant, **failure})
    if seen != expected:
        raise ValueError("missing reviews; partial coverage cannot pass")
    means = {g: {k: sum(v) / len(v) for k, v in dims.items()} for g, dims in quality.items()}
    regression = any(vals[f"candidate/{d}"] < vals[f"baseline/{d}"]
                     for vals in means.values() for d in DIMENSIONS)
    pair_regression = any(c["candidate"] < c["baseline"] for c in counts.values())
    return {"reviewed_pairs": len(seen), "pair_counts": dict(counts), "dimension_means": means,
            "hard_failures": failures,
            "within_packet_gate": not any(f["variant"] == "candidate" for f in failures)
            and not regression and not pair_regression,
            "release_approved": False,
            "limitation": "Packet checks do not establish holdout coverage, pipeline correctness or population gains."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("pilot", "main", "holdout", "repeat", "fixed", "autonomy", "mood_probe", "smoke"), default="pilot")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases, fixture_hash = load_stage_cases(args.stage)
    plan = make_plan(cases, fixture_hash, args.stage)
    if args.output:
        args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in plan.items() if k != "pairs"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
