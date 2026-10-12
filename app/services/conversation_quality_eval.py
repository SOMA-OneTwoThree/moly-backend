"""Bounded, server-owned synthetic inputs for the authenticated development evaluator.

No database reads, user lookup, arbitrary system override or provider adapter lives here.
Only the four changed persona sections are retained from the pinned baseline.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re

from app.schemas.dev import QualityEvalCaseRequest
from app.services import mood_context, prompts

BASELINE_SHA = "6fb3ea72f9703456301d73dc66e7098ff792d3d2"
EVALS = Path(__file__).resolve().parents[2] / "evals"
CASE_IDS = frozenset(f"{lang}_{split}{i:02}" for lang in ("ko", "ja", "en")
                     for split, size in (("d", 8), ("h", 4), ("a", 3)) for i in range(1, size + 1))
MOOD_CASE_IDS = frozenset(f"{lang}_m0{i}" for lang in ("ko", "ja", "en") for i in (1, 2))
MAX_TOKENS = 384
MAX_WIRE_CHARS = 40_000
ACKS = {"ko": ("응. 듣고 있어.", "아 그렇구나.", "응."),
        "ja": ("うん。聞いてるよ。", "そうだったんだね。", "うん。"),
        "en": ("I'm listening.", "Oh I see.", "Okay.")}


class QualityFixtureError(ValueError):
    """Only a fixed error code is returned to the API; never include source contents."""


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def _fixtures(*, mood_probe: bool = False) -> tuple[dict, str]:
    # The supplemental one-turn bundle cannot alter the established four-turn hash.
    paths = ((EVALS / "conversation_quality_mood_probe_v1.json",) if mood_probe else
             (EVALS / "conversation_quality_v1.json", EVALS / "conversation_quality_holdout_v1.json",
              EVALS / "conversation_quality_autonomy_v1.json"))
    try:
        manifests = [json.loads(path.read_text()) for path in paths]
        cases = {}
        for manifest in manifests:
            if manifest["baseline_sha"] != BASELINE_SHA or manifest["data_origin"] != "new_synthetic_only":
                raise QualityFixtureError("fixture_identity")
            if mood_probe and manifest.get("split") != "mood_probe":
                raise QualityFixtureError("fixture_bundle")
            for case in manifest["cases"]:
                if case["id"] in cases or case["source"] != "new_synthetic":
                    raise QualityFixtureError("fixture_case")
                if len(case["turns"]) != (1 if mood_probe else 4) or case["setup"]["memories"]:
                    raise QualityFixtureError("fixture_shape")
                cases[case["id"]] = case
        if set(cases) != (MOOD_CASE_IDS if mood_probe else CASE_IDS):
            raise QualityFixtureError("fixture_coverage")
        return cases, _digest(manifests)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise QualityFixtureError("fixture_unavailable") from exc


def _baseline(candidate: str, locale: str) -> str:
    try:
        artifact = json.loads((EVALS / "conversation_quality_baseline6fb.json").read_text())
        if artifact["baseline_sha"] != BASELINE_SHA:
            raise QualityFixtureError("baseline_identity")
        expected = artifact["locales"][locale]
        result = candidate
        if len(expected["sections"]) != 4:
            raise QualityFixtureError("baseline_sections")
        for header, original in expected["sections"].items():
            pattern = re.compile(r"(?ms)^\[" + re.escape(header) + r"\].*?(?=^\[|\Z)")
            if len(pattern.findall(result)) != 1:
                raise QualityFixtureError("baseline_header")
            result = pattern.sub(lambda _: original, result, count=1)
        if hashlib.sha256(result.encode()).hexdigest() != expected["system_sha256"]:
            raise QualityFixtureError("baseline_hash")
        return result
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise QualityFixtureError("baseline_unavailable") from exc


def build_input(request: QualityEvalCaseRequest) -> tuple[str, list[dict], dict]:
    # Reuse the production clock renderer. Importing does not open any DB connection.
    from app.services.chat import _time_context, _time_label

    mood_probe = request.case_id in MOOD_CASE_IDS
    cases, fixture_hash = _fixtures(mood_probe=mood_probe)
    if request.fixture_hash != fixture_hash or request.case_id not in cases:
        raise QualityFixtureError("fixture_mismatch")
    case = cases[request.case_id]
    setup = case["setup"]
    locale, mood = case["locale"], setup["mood"]
    start = datetime.fromisoformat(setup["at"])
    if start.tzinfo is None:
        raise QualityFixtureError("fixture_clock")
    messages, fixed_history = [], []
    previous = None
    for index, turn in enumerate(case["turns"][:request.turn + 1]):
        locale, mood = turn.get("locale", locale), turn.get("mood", mood)
        if locale not in ACKS or mood not in {*mood_context._MOOD_LABELS, "unknown"}:
            raise QualityFixtureError("fixture_state")
        now = start + timedelta(minutes=turn["minutes_after_start"])
        user = {"role": "user", "content": turn["text"]}
        fixed_history.append(user)
        messages.append({**user, "content": f"{_time_label(now, setup['timezone'])}\n{turn['text']}"})
        if index < request.turn:
            response = (ACKS[case["locale"]][index] if request.mode == "fixed_history"
                        else request.prior_assistant_outputs[index])
            assistant = {"role": "assistant", "content": response}
            fixed_history.append(assistant)
            messages.append(assistant)
            previous = now
    labels = mood_context._MOOD_LABELS.get(mood)
    label = labels[locale] if labels else "unknown"
    # Label rendering shares the production mapping; this mode deliberately never calls
    # today_block(), which would read a live account. Dates come only from the fixture.
    clock = _time_context(now, previous, setup["timezone"])
    mood_block = f"[User mood selection] {now.date().isoformat()}: {label}"
    messages.insert(len(messages) - 1, {"role": "system", "content": f"{clock}\n\n{mood_block}"})
    candidate = prompts.system_prompt(locale)
    baseline = _baseline(candidate, locale)  # Verify BOTH arms, never compare a drifting baseline.
    system = baseline if request.variant == "baseline6fb" else candidate
    wire = {"system": system, "messages": messages}
    if len(json.dumps(wire, ensure_ascii=False)) > MAX_WIRE_CHARS:
        raise QualityFixtureError("wire_bound")
    metadata = {"scope": "source_prompt", "baseline_sha": BASELINE_SHA,
                "fixture_hash": fixture_hash, "case_id": request.case_id, "turn": request.turn,
                "variant": request.variant, "mode": request.mode, "locale": locale,
                "system_hash": hashlib.sha256(system.encode()).hexdigest(), "wire_hash": _digest(wire),
                "provider_attempts_upper": 3, "wire_chars": len(json.dumps(wire, ensure_ascii=False))}
    if request.mode == "fixed_history":
        metadata["history_hash"] = _digest(fixed_history)
    return system, messages, metadata
