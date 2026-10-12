"""Explicitly gated synthetic generation through the existing development API only.

Default invocation makes no network request. No provider key or account lookup is used.
The operator supplies a bearer token through MOLY_QUALITY_EVAL_BEARER; it is never saved.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import sys
from statistics import median

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.schemas.dev import QualityEvalCaseRequest  # noqa: E402
from app.services import conversation_quality_eval as quality  # noqa: E402
from scripts import eval_conversation_quality as evaluation  # noqa: E402

DEV_URL = "https://dev.moly.asia"
PREVIOUS_APPROVAL = "root-20261012-mood-coverage492"
POLICY_APPROVAL = "root-20261012-candidate-revalidation600"
LEGACY_LIMITS = {"dollars": "15", "generation_turns": 480, "attempts": 1500,
                 "input_tokens": 50_000_000, "output_tokens": 600_000}
PREVIOUS_LIMITS = {**LEGACY_LIMITS, "generation_turns": 492, "attempts": 1600}


def load_prices(path: Path, model: str, *, now: datetime) -> dict:
    value = json.loads(path.read_text())
    if set(value) != {"model", "source", "verified_at", "input_upper_usd_per_million",
                      "output_upper_usd_per_million"}:
        raise ValueError("a reviewed price manifest with explicit upper rates is required")
    if value["model"] != model or not isinstance(value["source"], str) or not value["source"].strip():
        raise ValueError("price/model provenance mismatch")
    verified = datetime.fromisoformat(value["verified_at"])
    if verified.tzinfo is None or not 0 <= (now - verified).total_seconds() <= 7 * 86400:
        raise ValueError("prices must have been verified in the previous seven days")
    for field in ("input_upper_usd_per_million", "output_upper_usd_per_million"):
        rate = Decimal(str(value[field]))
        if not rate.is_finite() or rate <= 0:
            raise ValueError("unknown/zero prices cannot authorize generation")
    return value


def reservation(system: str, messages: list[dict], prices: dict) -> dict:
    # Deliberately loose byte upper bound plus role/framing allowance. Every possible
    # SDK attempt is reserved; observed usage is reported separately, never refunded.
    input_bound = len(system.encode()) + sum(len(m["content"].encode()) for m in messages) + 1024
    attempts = 3
    in_tokens, out_tokens = input_bound * attempts, quality.MAX_TOKENS * attempts
    dollars = (Decimal(in_tokens) * Decimal(str(prices["input_upper_usd_per_million"]))
               + Decimal(out_tokens) * Decimal(str(prices["output_upper_usd_per_million"]))) / 1_000_000
    return {"dollars": dollars, "attempts": attempts,
            "input_tokens": in_tokens, "output_tokens": out_tokens}


def save_private(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _recompute_budget(calls: list[dict], *, limits: dict) -> evaluation.Budget:
    if any(c["status"] == "reserved" for c in calls):
        raise ValueError("unresolved reservation or unknown ledger; reconcile without replay")
    budget, keys = evaluation.Budget(), set()
    for call in calls:
        if call["status"] not in {"completed", "failed_unknown_usage"} or call["key"] in keys:
            raise ValueError("invalid or duplicate ledger entry")
        keys.add(call["key"])
        budget.reserve(dollars=Decimal(call["reserved_usd"]), attempts=call["attempts_upper"],
                       input_tokens=call["input_tokens_upper"], output_tokens=call["output_tokens_upper"])
    if any(Decimal(str(value)) > Decimal(str(limits[key]))
           for key, value in _budget_snapshot(budget).items()):
        raise ValueError("ledger exceeded its approved policy")
    return budget


def _budget_snapshot(budget: evaluation.Budget) -> dict:
    return {**asdict(budget), "dollars": str(budget.dollars)}


def _policy_event(kind: str, approval: str = POLICY_APPROVAL) -> dict:
    return {"kind": kind, "approval": approval,
            "recorded_at": datetime.now(timezone.utc).isoformat()}


def _validate_preserved_prefix(event: dict, data: dict, limits: dict) -> None:
    count = event.get("preserved_calls_count")
    if type(count) is not int or not 0 <= count <= len(data["calls"]):
        raise ValueError("invalid preserved reservation prefix")
    prefix = data["calls"][:count]
    if (event.get("preserved_calls_hash") != evaluation.digest(prefix)
            or event.get("preserved_budget") != _budget_snapshot(_recompute_budget(prefix, limits=limits))
            or event.get("price_manifest_hash") != data.get("price_manifest_hash")
            or any(not re.fullmatch(r"[a-f0-9]{64}", str(event.get(k, "")))
                   for k in ("source_ledger_hash", "source_capture_hash"))):
        raise ValueError("preserved reservation or provenance mismatch")


def _read_ledger(path: Path, *, policy_version: int) -> tuple[dict, evaluation.Budget]:
    if policy_version not in (2, 3):
        raise ValueError("unknown fixed policy")
    limits = PREVIOUS_LIMITS if policy_version == 2 else evaluation.BUDGET_LIMITS
    approval = PREVIOUS_APPROVAL if policy_version == 2 else POLICY_APPROVAL
    if not path.exists():
        return {"version": policy_version, "limits": dict(limits), "calls": [],
                "budget": {}, "price_manifest_hash": None,
                "policy_history": [_policy_event("initial", approval)]}, evaluation.Budget()
    data = json.loads(path.read_text())
    if data.get("version") != policy_version or data.get("limits") != limits:
        raise ValueError("explicit approved ledger policy upgrade is required")
    if not isinstance(data.get("calls"), list):
        raise ValueError("unknown ledger format")
    budget = _recompute_budget(data["calls"], limits=limits)
    if data["budget"] != _budget_snapshot(budget):
        raise ValueError("ledger totals do not match reservations")
    history = data.get("policy_history")
    if not isinstance(history, list) or not history:
        raise ValueError("approved policy history is required")
    for event in history:
        if datetime.fromisoformat(event["recorded_at"]).tzinfo is None:
            raise ValueError("policy time must include its timezone")
    # Only the two reviewed transitions exist. No arbitrary limits or approval text.
    if policy_version == 3 and len(history) == 1:
        if history[0].get("kind") != "initial" or history[0].get("approval") != POLICY_APPROVAL:
            raise ValueError("unknown initial policy")
    else:
        if len(history) != (1 if policy_version == 2 else 2):
            raise ValueError("unknown policy transition count")
        first = history[0]
        if first.get("approval") != PREVIOUS_APPROVAL:
            raise ValueError("prior approval must be preserved")
        if first.get("kind") == "upgrade_v1":
            if first.get("previous_attempt_limit") != 1500 or first.get("previous_generation_limit") != 480:
                raise ValueError("unknown legacy limits")
            _validate_preserved_prefix(first, data, LEGACY_LIMITS)
        elif first.get("kind") != "initial":
            raise ValueError("unknown prior policy")
        if policy_version == 3:
            latest = history[1]
            if (latest.get("kind") != "upgrade_v2" or latest.get("approval") != POLICY_APPROVAL
                    or latest.get("previous_limits") != PREVIOUS_LIMITS):
                raise ValueError("unknown revalidation transition")
            _validate_preserved_prefix(latest, data, PREVIOUS_LIMITS)
            if latest["preserved_calls_count"] < first.get("preserved_calls_count", 0):
                raise ValueError("policy history cannot lose prior reservations")
    if data["calls"] and not re.fullmatch(r"[a-f0-9]{64}", str(data.get("price_manifest_hash", ""))):
        raise ValueError("reviewed campaign prices must be pinned")
    return data, budget


def read_ledger(path: Path) -> tuple[dict, evaluation.Budget]:
    return _read_ledger(path, policy_version=3)


def upgrade_ledger_policy(path: Path, prices_path: Path, prior_capture_path: Path) -> None:
    """Offline, explicit, one-time upgrade; never refunds or replays a reservation."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a") as lock:
        lock_path.chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        raw = path.read_bytes()
        data = json.loads(raw)
        if data.get("version") != 1 or set(data) != {"version", "calls", "budget"}:
            raise ValueError("only the preserved legacy policy can be upgraded")
        budget = _recompute_budget(data["calls"], limits=LEGACY_LIMITS)
        if data["budget"] != _budget_snapshot(budget):
            raise ValueError("legacy totals do not match reservations")
        prices = load_prices(prices_path, "gpt-6-luna", now=datetime.now(timezone.utc))
        price_hash = evaluation.digest(prices)
        capture_raw = prior_capture_path.read_bytes()
        capture = json.loads(capture_raw)
        if (capture.get("price_manifest_hash") != price_hash
                or capture.get("operational_summary", {}).get("campaign_reserved") != data["budget"]
                or not all(c["key"].startswith(f"{capture['candidate_sha']}:{capture['stage']}:")
                           for c in data["calls"])):
            raise ValueError("existing capture must establish the preserved campaign provenance")
        event = {**_policy_event("upgrade_v1", PREVIOUS_APPROVAL),
                 "previous_attempt_limit": 1500, "previous_generation_limit": 480,
                 "source_ledger_hash": hashlib.sha256(raw).hexdigest(),
                 "source_capture_hash": hashlib.sha256(capture_raw).hexdigest(),
                 "preserved_calls_count": len(data["calls"]),
                 "preserved_calls_hash": evaluation.digest(data["calls"]),
                 "preserved_budget": dict(data["budget"]), "price_manifest_hash": price_hash}
        # The original bytes and upgraded ledger are taken under the SAME lock.
        # A crash after backup stops a second upgrade for explicit reconciliation.
        backup = path.with_suffix(path.suffix + ".v1-backup")
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        data.update(version=2, limits=dict(PREVIOUS_LIMITS),
                    price_manifest_hash=price_hash, policy_history=[event])
        save_private(path, data)
        _read_ledger(path, policy_version=2)


def upgrade_revalidation_policy(path: Path, prices_path: Path, prior_capture_path: Path) -> None:
    """Preserve the first candidate's evidence before the explicitly funded recheck."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a") as lock:
        lock_path.chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        data, _ = _read_ledger(path, policy_version=2)
        raw = path.read_bytes()
        prices = load_prices(prices_path, "gpt-6-luna", now=datetime.now(timezone.utc))
        price_hash = evaluation.digest(prices)
        capture_raw = prior_capture_path.read_bytes()
        capture = json.loads(capture_raw)
        if (data["price_manifest_hash"] != price_hash or capture.get("price_manifest_hash") != price_hash
                or capture.get("operational_summary", {}).get("campaign_reserved") != data["budget"]):
            raise ValueError("existing capture must establish the preserved campaign provenance")
        event = {**_policy_event("upgrade_v2"), "previous_limits": dict(PREVIOUS_LIMITS),
                 "source_ledger_hash": hashlib.sha256(raw).hexdigest(),
                 "source_capture_hash": hashlib.sha256(capture_raw).hexdigest(),
                 "preserved_calls_count": len(data["calls"]),
                 "preserved_calls_hash": evaluation.digest(data["calls"]),
                 "preserved_budget": dict(data["budget"]), "price_manifest_hash": price_hash}
        backup = path.with_suffix(path.suffix + ".v2-backup")
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        data.update(version=3, limits=dict(evaluation.BUDGET_LIMITS))
        data["policy_history"].append(event)
        save_private(path, data)
        read_ledger(path)


def _save_ledger(path: Path, ledger: dict, budget: evaluation.Budget) -> None:
    ledger["budget"] = {**asdict(budget), "dollars": str(budget.dollars)}
    save_private(path, ledger)


def operational_summary(capture: dict, ledger: dict, budget: evaluation.Budget) -> dict:
    """Keep unsuccessful attempts and missing measurements in each arm's denominator."""
    prefix = f"{capture['candidate_sha']}:{capture['stage']}:"
    calls = [call for call in ledger["calls"] if call["key"].startswith(prefix)]
    arms = {}
    for variant in ("baseline", "candidate"):
        branch = [call for call in calls if call["key"].split(":")[-2] == variant]
        observed = [call.get("observed_usage") or {} for call in branch]
        fields = {}
        for key in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens", "latency_ms"):
            known = [row[key] for row in observed if type(row.get(key)) in (int, float)
                     and math.isfinite(row[key]) and row[key] >= 0]
            fields[key] = {"known_calls": len(known), "unknown_calls": len(branch) - len(known),
                           "known_sum": sum(known) if known else None,
                           "known_mean": sum(known) / len(known) if known else None}
            if key == "latency_ms":
                fields[key].update(median=median(known) if known else None,
                                   p95=sorted(known)[math.ceil(len(known) * .95) - 1] if known else None)
        arms[variant] = {"reserved_calls": len(branch),
                         "completed_calls": sum(c["status"] == "completed" for c in branch),
                         "failed_or_unknown_calls": sum(c["status"] != "completed" for c in branch),
                         "reserved_usd": str(sum((Decimal(c["reserved_usd"]) for c in branch), Decimal(0))),
                         "observed": fields}
    return {"arms": arms, "campaign_reserved": {**asdict(budget), "dollars": str(budget.dollars)},
            "provider_attempts": "reserved upper bounds; SDK attempts not independently observed",
            "latency": "successful measured calls only; small-sample p95 is not a service SLO",
            "cost": "uncached upper reservations, not invoiced or refunded costs"}


async def execute(args) -> dict:
    if args.stage == "smoke":
        raise ValueError("actual chat/dev smoke needs its separately approved persistence runner")
    if not re.fullmatch(r"[a-f0-9]{40}", args.expected_deployment_sha):
        raise ValueError("an exact deployment commit is required")
    token = os.environ.get("MOLY_QUALITY_EVAL_BEARER")
    if not token:
        raise ValueError("MOLY_QUALITY_EVAL_BEARER is required")
    prices = load_prices(args.prices, args.model, now=datetime.now(timezone.utc))
    if args.output.exists():
        raise ValueError("capture already exists; preserve earlier evidence")
    cases, fixture_hash = evaluation.load_stage_cases(args.stage)
    plan = evaluation.make_plan(cases, fixture_hash, args.stage)
    ledger, budget = read_ledger(args.ledger)
    price_hash = evaluation.digest(prices)
    if ledger.get("price_manifest_hash") not in (None, price_hash):
        raise ValueError("campaign prices changed; a new price policy must be reviewed")
    ledger["price_manifest_hash"] = price_hash
    prefix = f"{args.expected_deployment_sha}:{args.stage}:"
    if any(c["key"].startswith(prefix) for c in ledger["calls"]):
        raise ValueError("this stage already has reservations; do not silently replay it")
    capture = {"version": 1, "scope": "source_prompt", "baseline_sha": evaluation.BASELINE_SHA,
               "candidate_sha": args.expected_deployment_sha, "fixture_hash": fixture_hash,
               "stage": args.stage, "price_manifest_hash": evaluation.digest(prices),
               "model_config_hash": None, "records": [], "failures": [],
               "planned_generation_turns": plan["generation_turns"], "completed": False}
    grouped = {}
    for item in plan["pairs"]:
        grouped.setdefault(item["case_id"], []).append(item)
    rng = random.Random(args.seed)
    order = list(grouped)
    rng.shuffle(order)
    # Sequential requests avoid extra load and make pre-call reservation atomic.
    # The same single client uses TLS verification and never follows redirects.
    async with httpx.AsyncClient(base_url=DEV_URL, timeout=210, follow_redirects=False, trust_env=False,
                                 headers={"Authorization": f"Bearer {token}"}) as client:
        health = await client.get("/health")
        if health.status_code != 200:
            raise ValueError("development health preflight failed")
        live = health.json()
        if live.get("env") != "development" or live.get("version") != args.expected_deployment_sha:
            raise ValueError("development deployment does not match the approved candidate")
        for case_id in order:
            items = grouped[case_id]
            rng.shuffle(items)
            rows = {}
            for item in items:
                row = {"case_id": case_id, "variant": item["variant"], "mode": plan["mode"],
                       "repetition": int(args.stage == "repeat"), "outputs": [], "measurements": []}
                capture["records"].append(row)
                rows[item["variant"]] = row
            for turn in items[0]["turns"]:
                for item in items:
                    row = rows[item["variant"]]
                    if row.get("error"):
                        continue
                    request = QualityEvalCaseRequest(
                        case_id=case_id, turn=turn,
                        variant="baseline6fb" if item["variant"] == "baseline" else "candidate",
                        mode=plan["mode"], fixture_hash=fixture_hash,
                        prior_assistant_outputs=row["outputs"] if plan["mode"] == "rollout" else [],
                    )
                    system, messages, expected = quality.build_input(request)
                    reserved = reservation(system, messages, prices)
                    try:
                        budget.reserve(**reserved)
                    except ValueError:
                        capture["stop_reason"] = "budget_guard"
                        capture["operational_summary"] = operational_summary(capture, ledger, budget)
                        save_private(args.output, capture)
                        return capture
                    event = {"key": f"{prefix}{case_id}:{item['variant']}:{turn}",
                             "status": "reserved", "reserved_usd": str(reserved["dollars"]),
                             "attempts_upper": reserved["attempts"],
                             "input_tokens_upper": reserved["input_tokens"],
                             "output_tokens_upper": reserved["output_tokens"], "observed_usage": None}
                    ledger["calls"].append(event)
                    _save_ledger(args.ledger, ledger, budget)
                    try:
                        response = await client.post("/dev/chat-eval", json={
                            "provider": "openai", "model": args.model, "max_tokens": quality.MAX_TOKENS,
                            "quality": request.model_dump(),
                        })
                        if response.status_code != 200:
                            raise ValueError(f"http_{response.status_code}")
                        data = response.json()
                        metadata = data.get("quality") or {}
                        if any(metadata.get(k) != expected[k] for k in (
                            "fixture_hash", "wire_hash", "system_hash", "provider_attempts_upper",
                        )) or metadata.get("deployment_sha") != args.expected_deployment_sha:
                            raise ValueError("response_identity_mismatch")
                        if not re.fullmatch(r"[a-f0-9]{64}", str(metadata.get("model_config_hash", ""))):
                            raise ValueError("model_configuration_missing")
                        if capture["model_config_hash"] not in (None, metadata.get("model_config_hash")):
                            raise ValueError("model_configuration_changed")
                        capture["model_config_hash"] = metadata["model_config_hash"]
                        if data.get("error") or not (data.get("text") or "").strip():
                            raise ValueError("model_failed_or_empty")
                        if data.get("finish_reason") == "length":
                            raise ValueError("model_output_truncated")
                        if not data.get("model_snapshot"):
                            raise ValueError("model_snapshot_missing")
                        if any(r.get("model_snapshot", data["model_snapshot"]) != data["model_snapshot"]
                               for r in capture["records"]):
                            raise ValueError("model_snapshot_changed")
                        row["model_snapshot"] = data["model_snapshot"]
                        row["outputs"].append(data["text"])
                        if "history_hash" in metadata:
                            row["history_hash"] = metadata["history_hash"]
                        measurement = {k: data.get(k) for k in (
                            "input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens",
                            "cache_write_estimated", "latency_ms", "finish_reason",
                        )}
                        row["measurements"].append(measurement)
                        event["observed_usage"] = measurement
                        event["status"] = "completed"
                    except Exception as exc:  # no raw exception, response, URL or authorization logging
                        code = str(exc) if type(exc) is ValueError and str(exc) in {
                            "response_identity_mismatch", "model_configuration_changed", "model_configuration_missing",
                            "model_failed_or_empty",
                            "model_output_truncated", "model_snapshot_missing", "model_snapshot_changed",
                        } else type(exc).__name__
                        event["status"] = "failed_unknown_usage"
                        row["error"] = code
                        capture["failures"].append({"case_id": case_id, "variant": item["variant"],
                                                    "turn": turn, "code": code})
                    _save_ledger(args.ledger, ledger, budget)
                    capture["operational_summary"] = operational_summary(capture, ledger, budget)
                    save_private(args.output, capture)
                    print(f"{args.stage}: reserved {budget.generation_turns} turns; {event['status']}", flush=True)
                    if event["status"] != "completed":
                        return capture  # pilot failures are investigated before spending on more cases
    capture["completed"] = True
    save_private(args.output, capture)
    return capture


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-approved-stage", action="store_true")
    parser.add_argument("--upgrade-attempt-policy-1600", action="store_true")
    parser.add_argument("--upgrade-revalidation-policy-600", action="store_true")
    parser.add_argument("--prior-capture", type=Path)
    parser.add_argument("--stage", choices=("pilot", "main", "holdout", "repeat", "fixed", "autonomy", "mood_probe", "smoke"), default="pilot")
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--expected-deployment-sha")
    parser.add_argument("--prices", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed", type=int, default=2718)
    args = parser.parse_args()
    if args.upgrade_attempt_policy_1600 or args.upgrade_revalidation_policy_600:
        if ((args.upgrade_attempt_policy_1600 and args.upgrade_revalidation_policy_600)
                or args.execute_approved_stage or args.output or args.expected_deployment_sha
                or not all((args.ledger, args.prices, args.prior_capture))):
            parser.error("offline upgrade requires only ledger, reviewed prices and prior capture")
        try:
            upgrade = upgrade_revalidation_policy if args.upgrade_revalidation_policy_600 else upgrade_ledger_policy
            upgrade(args.ledger, args.prices, args.prior_capture)
        except Exception:
            raise SystemExit("Policy upgrade rejected; existing reservations were not reset.") from None
        print("Approved policy transition recorded; no network calls or reservation refunds.")
        return
    if not args.execute_approved_stage:
        cases, fixture_hash = evaluation.load_stage_cases(args.stage)
        plan = evaluation.make_plan(cases, fixture_hash, args.stage)
        print(json.dumps({k: v for k, v in plan.items() if k != "pairs"}))
        return
    if any(getattr(args, key) is None for key in ("expected_deployment_sha", "prices", "ledger", "output")):
        parser.error("execution requires deployment SHA, reviewed prices, shared ledger and output")
    if args.ledger.resolve() == args.output.resolve():
        parser.error("ledger and response artifact must be separate")
    lock_path = args.ledger.with_suffix(args.ledger.suffix + ".lock")
    try:
        with lock_path.open("a") as lock:
            lock_path.chmod(0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            capture = asyncio.run(execute(args))
            if capture["failures"] or capture.get("stop_reason"):
                raise SystemExit("Stage stopped after a failed synthetic call; inspect the private report.")
    except Exception:  # even malformed server/ledger data must not print raw payloads
        raise SystemExit("Evaluation stopped by its preflight or budget guard; no exception contents emitted.") from None


if __name__ == "__main__":
    main()
