"""Production subscription operations. Invoked only by the fixed OIDC/SSM workflow.

No customer identifiers or provider responses leave the host. A private immutable
manifest fixes membership, before images and controls before the first repair.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
import logging
import os
from pathlib import Path
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from urllib.parse import unquote, urlsplit
import uuid

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.core.db import get_engine, get_sessionmaker
from app.models.app_config import AppConfig
from app.models.payment import Payment
from app.models.revenuecat_event import RevenuecatEvent
from app.models.subscription import Subscription
from app.services import subscription, subscription_access as access
from app.services import subscription_processing as processing, subscription_repair as repair

PRODUCTION_PROJECT = "qkgjlgzsharnilxnkytd"
ACTIONS = ("report", "repair_refunded", "repair_metadata", "verify", "activate", "deactivate")
_COHORT_FIELDS = ("mode", "subscription_id", "owner_id", "payment_id", "payment_tx", "payment_status")


class OperationStopped(Exception):
    pass


def require_runtime(expected_sha: str, *, require_provider: bool = True) -> None:
    parsed = urlsplit(re.sub(r"^postgresql\+asyncpg://", "postgresql://", settings.supabase_db_connection_string))
    if (settings.environment != "production" or settings.git_sha != expected_sha
            or not re.fullmatch(r"[0-9a-f]{40}", expected_sha)
            or parsed.scheme not in {"postgres", "postgresql"} or parsed.query or parsed.fragment
            or unquote(parsed.username or "") != "postgres." + PRODUCTION_PROJECT
            or parsed.path != "/postgres" or parsed.port not in {None, 5432, 6543}
            or not (parsed.hostname or "").endswith(".pooler.supabase.com")
            or urlsplit(settings.supabase_url).hostname != PRODUCTION_PROJECT + ".supabase.co"):
        raise OperationStopped("runtime_target_mismatch")
    if require_provider and (not settings.revenuecat_api_v2_key or not settings.revenuecat_project_id):
        raise OperationStopped("provider_read_settings_missing")


def write_private(path: Path, value: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(repair.serial(value), stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def append_private(path: Path, value: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as stream:
        stream.write(json.dumps(repair.serial(value), sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def membership(manifest: dict) -> str:
    return repair.digest([{key: item[key] for key in _COHORT_FIELDS} for item in manifest["targets"]])


async def config_values(session, *, lock: bool = False) -> dict:
    query = select(AppConfig).where(AppConfig.key.in_((processing.ENABLED, processing.COHORT))).order_by(AppConfig.key)
    if lock:
        query = query.with_for_update()
    rows = (await session.execute(query)).scalars()
    return {row.key: deepcopy(row.value) for row in rows}


async def commit_configuration(session, folder: Path, before: dict, after: dict) -> None:
    path = folder / "configuration.jsonl"
    append_private(path, {"status": "commit_pending", "before": before, "after": after})
    try:
        await session.commit()
    except BaseException:
        try:
            append_private(path, {"status": "commit_result_unknown"})
        except OSError:
            pass
        raise OperationStopped("configuration_commit_unknown") from None
    try:
        append_private(path, {"status": "committed", "after": after,
                              "at": datetime.now(timezone.utc).isoformat()})
    except OSError:
        raise OperationStopped("configuration_committed_audit_failed") from None


async def control_record(session, sub: Subscription, pay: Payment) -> dict:
    return {"subscription_id": str(sub.id), "before": repair.serial(processing.baseline(sub)),
            "payment_id": str(pay.id), "payment_status": pay.status,
            "payment_tx": pay.store_transaction_id, "amount": str(pay.amount), "currency": pay.currency}


async def collect_manifest(session, expected_sha: str) -> dict:
    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
    subs = list((await session.execute(select(Subscription).where(
        Subscription.environment == "PRODUCTION").order_by(Subscription.id))).scalars())
    targets, controls = [], []
    for sub in subs:
        pay = (await session.execute(select(Payment).where(Payment.subscription_id == sub.id)
                                    .order_by(Payment.paid_at.desc().nullslast(), Payment.id).limit(1))).scalars().first()
        if pay is None:
            continue
        trial = (await session.execute(select(RevenuecatEvent.event_id).where(
            RevenuecatEvent.payload["type"].astext == "INITIAL_PURCHASE",
            RevenuecatEvent.payload["period_type"].astext == "TRIAL",
            RevenuecatEvent.payload["transaction_id"].astext == sub.original_transaction_id,
            RevenuecatEvent.payload["environment"].astext == "PRODUCTION",
        ).limit(1))).scalars().first()
        affected = (sub.plan == "yearly" and trial is not None and sub.access_evidence is None
                    and sub.status in {"active", "grace_period"}
                    and sub.latest_transaction_id != pay.store_transaction_id and sub.store_trial_ends_at is not None)
        if affected:
            if pay.user_id != sub.user_id or pay.status not in {"paid", "refunded"} or sub.access_evidence is not None:
                raise OperationStopped("cohort_requires_manual_review")
            targets.append({"mode": "refunded" if pay.status == "refunded" else "metadata",
                            "subscription_id": str(sub.id), "owner_id": str(sub.user_id),
                            "payment_id": str(pay.id), "payment_tx": pay.store_transaction_id,
                            "payment_status": pay.status, "before": repair.serial(processing.baseline(sub)),
                            "financial_sha256": await repair._financial_digest(session, sub)})
        else:
            controls.append(await control_record(session, sub, pay))
    result = {"version": 1, "environment": "production", "project_ref": PRODUCTION_PROJECT,
              "app_sha": expected_sha, "created_at": datetime.now(timezone.utc).isoformat(),
              "targets": targets, "controls": controls, "configuration_before": await config_values(session)}
    await session.rollback()
    return result


async def verify_controls(session, manifest: dict) -> None:
    for record in manifest["controls"]:
        sub = await session.get(Subscription, uuid.UUID(record["subscription_id"]), populate_existing=True)
        pay = await session.get(Payment, uuid.UUID(record["payment_id"]), populate_existing=True)
        if sub is None or pay is None or await control_record(session, sub, pay) != record:
            raise OperationStopped("control_subscription_changed")


def last_committed(path: Path) -> dict | None:
    if not path.exists():
        return None
    last = None
    for line in path.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            raise OperationStopped("private_journal_requires_review") from None
        if entry.get("status") in {"commit_pending", "commit_result_unknown"}:
            last = entry
        elif entry.get("status") == "committed":
            last = entry
    if last and (last.get("status") != "committed" or last.get("postcheck") != "verified"):
        raise OperationStopped("previous_commit_requires_review")
    return last


def bind_target_plan(plan: dict, target: dict, before: dict) -> None:
    if (plan.get("before") != before or plan.get("owner_id") != target["owner_id"]
            or plan.get("subscription_id") != target["subscription_id"] or plan.get("mode") != target["mode"]
            or plan.get("payment_id") != target["payment_id"] or plan.get("payment_tx") != target["payment_tx"]
            or plan.get("financial_sha256") != target["financial_sha256"]):
        raise OperationStopped("fresh_plan_changed_from_manifest")


async def verify_targets(session, manifest: dict, folder: Path, *, fresh_provider: bool = False) -> None:
    for index, target in enumerate(manifest["targets"]):
        receipt = last_committed(folder / f"target-{index}.jsonl")
        if receipt is None:
            raise OperationStopped("cohort_repair_incomplete")
        sub = await session.get(Subscription, uuid.UUID(target["subscription_id"]), populate_existing=True)
        pay = await session.get(Payment, uuid.UUID(target["payment_id"]), populate_existing=True)
        if (sub is None or repair.serial(processing.baseline(sub)) != receipt["after"]
                or pay is None or pay.status != target["payment_status"]
                or await repair._financial_digest(session, sub) != target["financial_sha256"]):
            raise OperationStopped("repaired_subscription_changed")
        current = access.resolve_access(sub, datetime.now(timezone.utc))
        if target["mode"] == "refunded":
            if current is not None:
                raise OperationStopped("refunded_access_remains")
        elif (current is None or current.store_trial_ends_at is not None
              or sub.expires_at.isoformat() != target["before"]["expires_at"]):
            raise OperationStopped("paid_access_not_preserved")
        if await repair._pending(session, sub, [target["payment_tx"]]):
            raise OperationStopped("related_inbox_changed")
        if fresh_provider:
            await session.rollback()
            plan = await repair.preview(session, uuid.UUID(target["subscription_id"]), target["mode"],
                                        project_ref=PRODUCTION_PROJECT, app_sha=manifest["app_sha"], environment="production")
            bind_target_plan(plan, target, receipt["after"])


async def operate(args) -> dict:
    require_runtime(args.expected_sha, require_provider=args.action != "deactivate")
    folder = Path("/audit") / args.session
    if folder.is_symlink():
        raise OperationStopped("invalid_private_audit_directory")
    folder.mkdir(mode=0o700, exist_ok=True)
    manifest_path = folder / "manifest.json"
    factory = get_sessionmaker()
    get_engine().sync_engine.hide_parameters = True
    async with factory() as session:
        if args.action == "deactivate":
            # A later compatible release must be able to stop NEW adoption even
            # when an old customer manifest no longer matches its release SHA.
            await session.execute(text("SET LOCAL lock_timeout = '2s'"))
            await subscription._ownership_lock(session)
            before = await config_values(session, lock=True)
            row = await session.get(AppConfig, processing.ENABLED)
            if row is not None:
                row.value = False
            after = {**before, processing.ENABLED: False} if row is not None else before
            await commit_configuration(session, folder, before, after)
            return {"ok": True, "action": args.action, "sha": args.expected_sha,
                    "new_adoption_enabled": False}
        if args.action == "report":
            manifest = await collect_manifest(session, args.expected_sha)
            if not manifest["targets"]:
                raise OperationStopped("no_matching_cohort")
            counts = {mode: sum(t["mode"] == mode for t in manifest["targets"]) for mode in ("refunded", "metadata")}
            if (membership(manifest) != args.expected_membership
                    or [counts["refunded"], counts["metadata"], len(manifest["controls"])] != args.expected_counts):
                raise OperationStopped("cohort_membership_or_counts_changed")
            # A real project read proves credentials and connectivity without exposing either.
            first = manifest["targets"][0]
            current, _ = await subscription._snapshot_for_transaction(first["payment_tx"], "PRODUCTION")
            if current.get("customer_id") != first["owner_id"]:
                raise OperationStopped("provider_ownership_changed")
            write_private(manifest_path, manifest)
        else:
            manifest = json.loads(manifest_path.read_text())
            if (manifest.get("version") != 1 or manifest.get("environment") != "production"
                    or manifest.get("app_sha") != args.expected_sha
                    or manifest.get("project_ref") != PRODUCTION_PROJECT
                    or repair.digest(manifest) != args.expected_manifest
                    or membership(manifest) != args.expected_membership):
                raise OperationStopped("manifest_mismatch")
            if args.action != "deactivate":
                await verify_controls(session, manifest)
            if args.action in {"repair_refunded", "repair_metadata"}:
                if args.action == "repair_metadata":
                    for index, target in enumerate(manifest["targets"]):
                        if target["mode"] == "refunded" and last_committed(folder / f"target-{index}.jsonl") is None:
                            raise OperationStopped("refund_repair_must_finish_first")
                mode = "refunded" if args.action == "repair_refunded" else "metadata"
                for index, target in enumerate(manifest["targets"]):
                    if target["mode"] != mode:
                        continue
                    audit_path = folder / f"target-{index}.jsonl"
                    previous = last_committed(audit_path)
                    sub = await session.get(Subscription, uuid.UUID(target["subscription_id"]), populate_existing=True)
                    if sub is None:
                        raise OperationStopped("target_missing")
                    before = repair.serial(processing.baseline(sub))
                    if previous is not None:
                        if before != previous["after"]:
                            raise OperationStopped("completed_target_changed")
                        continue
                    if before != target["before"] or await repair._financial_digest(session, sub) != target["financial_sha256"]:
                        raise OperationStopped("target_changed_since_manifest")
                    await session.rollback()
                    plan = await repair.preview(session, uuid.UUID(target["subscription_id"]), mode, project_ref=PRODUCTION_PROJECT,
                                                app_sha=args.expected_sha, environment="production")
                    bind_target_plan(plan, target, target["before"])
                    # Keep every reviewed fresh plan before the first write, even on retry.
                    write_private(folder / f"target-{index}-plan-{uuid.uuid4().hex}.json", plan)
                    receipt = await repair.apply_preview(
                        session, plan, expected_sha256=repair.digest(plan), project_ref=PRODUCTION_PROJECT,
                        app_sha=args.expected_sha, environment="production",
                        on_status=lambda value, path=audit_path: append_private(path, value))
                    if receipt.get("postcheck") != "verified":
                        raise OperationStopped("committed_repair_requires_review")
                    await verify_controls(session, manifest)
                    await session.rollback()
            elif args.action in {"verify", "activate"}:
                await verify_targets(session, manifest, folder, fresh_provider=True)
                if args.action == "activate":
                    await session.rollback()
                    await session.execute(text("SET LOCAL lock_timeout = '2s'"))
                    await subscription._ownership_lock(session)
                    await verify_targets(session, manifest, folder)
                    await verify_controls(session, manifest)
                    current = await config_values(session, lock=True)
                    enabled = {processing.ENABLED: True, processing.COHORT: ["*"]}
                    if current not in (manifest["configuration_before"], enabled):
                        raise OperationStopped("activation_configuration_changed")
                    if current != enabled:
                        for key, value in enabled.items():
                            if key not in current:
                                inserted = (await session.execute(pg_insert(AppConfig).values(key=key, value=value)
                                            .on_conflict_do_nothing(index_elements=["key"]).returning(AppConfig.key))).scalar_one_or_none()
                                if inserted is None:
                                    raise OperationStopped("activation_configuration_changed")
                            else:
                                row = await session.get(AppConfig, key)
                                row.value = deepcopy(value)
                        await commit_configuration(session, folder, current, enabled)
        counts = {mode: sum(t["mode"] == mode for t in manifest["targets"]) for mode in ("refunded", "metadata")}
        counts["controls"] = len(manifest["controls"])
        configuration = await config_values(session)
        return {"ok": True, "action": args.action, "sha": args.expected_sha,
                "new_adoption_enabled": configuration.get(processing.ENABLED) is True,
                "manifest": repair.digest(manifest), "membership": membership(manifest), "counts": counts}


async def bounded_operation(args):
    async with asyncio.timeout(30 if args.action == "deactivate" else 420):
        return await operate(args)


def main() -> int:
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--expected-manifest", default="")
    parser.add_argument("--expected-membership", default="")
    parser.add_argument("--expected-counts", default="")
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            args = parser.parse_args()
            if (not re.fullmatch(r"[a-z0-9-]{8,80}", args.session)
                    or (args.action != "deactivate" and not re.fullmatch(r"[0-9a-f]{64}", args.expected_membership))
                    or (args.action not in {"report", "deactivate"} and not re.fullmatch(r"[0-9a-f]{64}", args.expected_manifest))):
                raise OperationStopped("invalid_operation_arguments")
            if args.action != "deactivate" and not re.fullmatch(r"\d{1,5},\d{1,5},\d{1,5}", args.expected_counts):
                raise OperationStopped("invalid_operation_arguments")
            args.expected_counts = [int(part) for part in args.expected_counts.split(",")] if args.expected_counts else []
            result = asyncio.run(bounded_operation(args))
        print(json.dumps(result, sort_keys=True))
        return 0
    except BaseException as exc:
        reason = str(exc) if isinstance(exc, OperationStopped) else "operation_failed_review_private_journal"
        print(json.dumps({"ok": False, "reason": reason}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
