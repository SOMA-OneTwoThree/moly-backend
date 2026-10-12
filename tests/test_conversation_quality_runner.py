"""No network: exercise persisted reservations and development identity with MockTransport."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from scripts import run_conversation_quality_dev as runner

SHA = "a" * 40


@pytest.fixture
def args(tmp_path, monkeypatch):
    monkeypatch.setenv("MOLY_QUALITY_EVAL_BEARER", "synthetic-bearer")
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({
        "model": "gpt-6-luna", "source": "synthetic-price-fixture-only",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "input_upper_usd_per_million": "0.1", "output_upper_usd_per_million": "0.2",
    }))
    return SimpleNamespace(stage="pilot", expected_deployment_sha=SHA, model="gpt-6-luna",
                           prices=prices, ledger=tmp_path / "ledger.json", output=tmp_path / "capture.json", seed=42)


@pytest.mark.parametrize("field,value", [
    ("input_upper_usd_per_million", "0"), ("output_upper_usd_per_million", "NaN"),
    ("model", "unreviewed-model"), ("source", ""),
    ("verified_at", (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()),
    ("verified_at", datetime.now().isoformat()),
])
def test_prices_fail_closed(args, field, value):
    prices = json.loads(args.prices.read_text())
    prices[field] = value
    args.prices.write_text(json.dumps(prices))
    with pytest.raises(ValueError):
        runner.load_prices(args.prices, args.model, now=datetime.now(timezone.utc))


def test_reservation_covers_three_attempts_and_full_output(args):
    prices = runner.load_prices(args.prices, args.model, now=datetime.now(timezone.utc))
    reserve = runner.reservation("가", [{"role": "user", "content": "나"}], prices)
    assert reserve == {"dollars": Decimal("0.0005394"), "attempts": 3,
                       "input_tokens": (6 + 1024) * 3, "output_tokens": 384 * 3}


def mock_client(monkeypatch, args, *, unhealthy=False, fail=False, snapshot_change=False):
    real_client = httpx.AsyncClient
    posts = []

    def handle(request):
        assert str(request.url).startswith(runner.DEV_URL)
        assert request.headers["Authorization"] == "Bearer synthetic-bearer"
        if request.url.path == "/health":
            return httpx.Response(200, json={"env": "development", "version": "b" * 40 if unhealthy else SHA})
        # The budget must already be durable before even a mocked provider request.
        ledger = json.loads(args.ledger.read_text())
        assert ledger["calls"][-1]["status"] == "reserved"
        assert len(ledger["calls"]) == len(posts) + 1
        body = json.loads(request.content)
        posts.append(body)
        if fail:
            raise httpx.ReadTimeout("synthetic-private-marker")
        quality_request = runner.QualityEvalCaseRequest(**body["quality"])
        _, _, metadata = runner.quality.build_input(quality_request)
        metadata.update(deployment_sha=SHA, model_config_hash="c" * 64)
        return httpx.Response(200, json={
            "quality": metadata, "text": "Synthetic response.", "finish_reason": "stop",
            "model_snapshot": "other-snapshot" if snapshot_change and len(posts) == 2 else "fixed-snapshot",
            "input_tokens": 100, "output_tokens": 5, "latency_ms": 10,
        })

    def client(**kw):
        assert kw["trust_env"] is False and kw["follow_redirects"] is False
        return real_client(**kw, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(runner.httpx, "AsyncClient", client)
    return posts


async def test_wrong_deployment_stops_before_generation(args, monkeypatch):
    posts = mock_client(monkeypatch, args, unhealthy=True)
    with pytest.raises(ValueError, match="deployment"):
        await runner.execute(args)
    assert not posts and not args.ledger.exists()


async def test_timeout_keeps_reservation_stops_stage_and_hides_exception(args, monkeypatch):
    posts = mock_client(monkeypatch, args, fail=True)
    capture = await runner.execute(args)
    ledger, budget = runner.read_ledger(args.ledger)
    assert len(posts) == 1 and budget.generation_turns == 1 and budget.attempts == 3
    assert budget.dollars > 0 and ledger["calls"][0]["status"] == "failed_unknown_usage"
    assert capture["failures"][0]["code"] == "ReadTimeout"
    summaries = capture["operational_summary"]["arms"].values()
    assert sum(s["failed_or_unknown_calls"] for s in summaries) == 1
    assert all(s["observed"]["input_tokens"]["known_sum"] is None for s in summaries)
    assert not capture["completed"]
    assert "synthetic-private-marker" not in args.output.read_text() + args.ledger.read_text()
    assert "synthetic-bearer" not in args.output.read_text() + args.ledger.read_text()
    assert args.ledger.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        await runner.execute(args)  # no implicit replay or evidence overwrite


async def test_snapshot_difference_between_arms_stops_immediately(args, monkeypatch):
    posts = mock_client(monkeypatch, args, snapshot_change=True)
    capture = await runner.execute(args)
    assert len(posts) == 2 and capture["failures"][0]["code"] == "model_snapshot_changed"


async def test_full_mock_pilot_pairs_and_shared_budget(args, monkeypatch):
    posts = mock_client(monkeypatch, args)
    capture = await runner.execute(args)
    assert len(posts) == 48 and not capture["failures"] and capture["completed"]
    for summary in capture["operational_summary"]["arms"].values():
        assert summary["reserved_calls"] == summary["completed_calls"] == 24
        assert summary["observed"]["input_tokens"]["known_sum"] == 2400
        assert summary["observed"]["cached_input_tokens"]["unknown_calls"] == 24
        assert summary["observed"]["latency_ms"]["p95"] == 10
    cases, fixture_hash = runner.evaluation.load_cases()
    packet, mapping = runner.evaluation.blind_pack(capture, cases, fixture_hash, seed=7)
    assert len(packet["pairs"]) == len(mapping["pairs"]) == 6
    _, budget = runner.read_ledger(args.ledger)
    assert budget.generation_turns == 48 and budget.attempts == 144
    assert budget.output_tokens == 48 * 384 * 3
    # Mutation of persisted totals or a crash before acknowledgment blocks resumption.
    ledger = json.loads(args.ledger.read_text())
    ledger["budget"]["dollars"] = "0"
    runner.save_private(args.ledger, ledger)
    with pytest.raises(ValueError, match="totals"):
        runner.read_ledger(args.ledger)
    ledger["calls"][-1]["status"] = "reserved"
    runner.save_private(args.ledger, ledger)
    with pytest.raises(ValueError, match="unresolved"):
        runner.read_ledger(args.ledger)


def test_default_cli_is_plan_only(monkeypatch, capsys):
    monkeypatch.setattr(runner.sys, "argv", [str(Path(runner.__file__))])
    monkeypatch.setattr(runner, "execute", lambda _: pytest.fail("default must not execute"))
    runner.main()
    output = json.loads(capsys.readouterr().out)
    assert output["generation_turns"] == 48 and output["execution_authorized"] is False


async def test_cost_cap_stops_before_post_and_reports_incomplete_scope(args, monkeypatch):
    prices = json.loads(args.prices.read_text())
    prices["input_upper_usd_per_million"] = "1000000"
    args.prices.write_text(json.dumps(prices))
    posts = mock_client(monkeypatch, args)
    capture = await runner.execute(args)
    assert not posts and not args.ledger.exists()
    assert capture["stop_reason"] == "budget_guard" and capture["completed"] is False
    assert capture["planned_generation_turns"] == 48
    assert capture["operational_summary"]["campaign_reserved"]["generation_turns"] == 0


def legacy_ledger(args, *, status="failed_unknown_usage", attempts=3):
    call = {"key": f"{SHA}:pilot:ko_d01:baseline:0", "status": status,
            "reserved_usd": "0.1", "attempts_upper": attempts,
            "input_tokens_upper": 300, "output_tokens_upper": 90, "observed_usage": None}
    budget = {"dollars": "0.1", "generation_turns": 1, "attempts": attempts,
              "input_tokens": 300, "output_tokens": 90}
    old = {"version": 1, "calls": [call], "budget": budget}
    runner.save_private(args.ledger, old)
    capture = {"candidate_sha": SHA, "stage": "pilot",
               "price_manifest_hash": runner.evaluation.digest(json.loads(args.prices.read_text())),
               "operational_summary": {"campaign_reserved": budget}}
    runner.save_private(args.output, capture)
    return old


def test_explicit_policy_upgrade_preserves_unknown_cost_and_original_bytes(args):
    old = legacy_ledger(args)
    original = args.ledger.read_bytes()
    with pytest.raises(ValueError, match="explicit"):
        runner.read_ledger(args.ledger)
    runner.upgrade_ledger_policy(args.ledger, args.prices, args.output)
    data, budget = runner.read_ledger(args.ledger)
    assert data["version"] == 2 and data["limits"]["attempts"] == 1600
    assert data["calls"] == old["calls"] and data["budget"] == old["budget"]
    assert budget.dollars == Decimal("0.1") and budget.attempts == 3
    event = data["policy_history"][0]
    assert event["source_ledger_hash"] == runner.hashlib.sha256(original).hexdigest()
    backup = args.ledger.with_suffix(args.ledger.suffix + ".v1-backup")
    assert backup.read_bytes() == original and backup.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="legacy"):
        runner.upgrade_ledger_policy(args.ledger, args.prices, args.output)
    # Even a self-consistent new total cannot alter the historical prefix.
    data["calls"][0]["reserved_usd"] = "0.09"
    data["budget"]["dollars"] = "0.09"
    runner.save_private(args.ledger, data)
    with pytest.raises(ValueError, match="preserved"):
        runner.read_ledger(args.ledger)


@pytest.mark.parametrize("problem", ["reserved", "old_cap", "price", "totals", "locked"])
def test_policy_upgrade_failures_preserve_legacy_without_backup(args, problem):
    legacy_ledger(args, status="reserved" if problem == "reserved" else "failed_unknown_usage",
                  attempts=1501 if problem == "old_cap" else 3)
    if problem == "price":
        data = json.loads(args.prices.read_text())
        data["input_upper_usd_per_million"] = "0.11"
        runner.save_private(args.prices, data)
    elif problem == "totals":
        data = json.loads(args.ledger.read_text())
        data["budget"]["dollars"] = "0"
        runner.save_private(args.ledger, data)
    original = args.ledger.read_bytes()
    lock_path = args.ledger.with_suffix(args.ledger.suffix + ".lock")
    with lock_path.open("a") as lock:
        if problem == "locked":
            runner.fcntl.flock(lock, runner.fcntl.LOCK_EX | runner.fcntl.LOCK_NB)
        with pytest.raises((ValueError, BlockingIOError)):
            runner.upgrade_ledger_policy(args.ledger, args.prices, args.output)
    assert args.ledger.read_bytes() == original
    assert not args.ledger.with_suffix(args.ledger.suffix + ".v1-backup").exists()


async def test_pinned_campaign_price_change_stops_before_network(args, monkeypatch):
    legacy_ledger(args)
    runner.upgrade_ledger_policy(args.ledger, args.prices, args.output)
    args.stage = "autonomy"
    args.output = args.output.with_name("next-stage.json")
    prices = json.loads(args.prices.read_text())
    prices["input_upper_usd_per_million"] = "0.11"
    runner.save_private(args.prices, prices)
    monkeypatch.setattr(runner.httpx, "AsyncClient", lambda **kw: pytest.fail("price mismatch must be offline"))
    with pytest.raises(ValueError, match="campaign prices changed"):
        await runner.execute(args)


def test_offline_policy_upgrade_cli_never_starts_execution(args, monkeypatch, capsys):
    legacy_ledger(args)
    monkeypatch.setattr(runner, "execute", lambda _: pytest.fail("upgrade must not execute"))
    monkeypatch.setattr(runner.sys, "argv", [str(Path(runner.__file__)),
        "--upgrade-attempt-policy-1600", "--ledger", str(args.ledger),
        "--prices", str(args.prices), "--prior-capture", str(args.output)])
    runner.main()
    assert "no network calls" in capsys.readouterr().out
