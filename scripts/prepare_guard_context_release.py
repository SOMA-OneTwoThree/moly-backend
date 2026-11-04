"""Live all-user private preparation. Default plan is read-only; no service pause.

Artifacts contain aggregates only. Existing public readers remain authoritative
until one atomic activation; no provider/model call is made by this command.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit
from sqlalchemy import text
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services import retention_release_promotion as promotion
# Explicit compatibility exports for existing offline/domain validation tools.
Blocked = promotion.Blocked
digest = promotion.digest
FORMAT = promotion.FORMAT
PROD_PROJECT_REF = "qkgjlgzsharnilxnkytd"
REQUIRED_COLUMNS = promotion.REQUIRED_COLUMNS
for _name in ("_USERS", "_MESSAGES", "_MEMORIES", "_SOURCES", "_CAS", "UserPlan",
              "read_user", "prepare_user", "audit_user", "schema_ready", "collect", "artifact",
              "approved_plan", "certified_canonical", "reverified_matches", "authorize_metadata"):
    globals()[_name] = getattr(promotion, _name)

def require_target(env, dsn, expected_ref, production_ack=False):
    from db.envfile import DEV_PROJECT_REFS
    if not re.fullmatch(r"[a-z0-9]{20}", expected_ref or ""):
        raise Blocked("expected_project_ref_required")
    try:
        u = urlsplit(dsn)
        user, host = unquote(u.username or ""), u.hostname or ""
        direct = host == f"db.{expected_ref}.supabase.co" and user == "postgres"
        pooler = (host.endswith(".pooler.supabase.com")
                  and user == f"postgres.{expected_ref}")
        valid = (u.scheme in {"postgres", "postgresql", "postgresql+asyncpg"}
                 and not u.query and not u.fragment and u.path == "/postgres"
                 and u.port in {5432, 6543} and (direct or pooler))
    except ValueError:
        valid = False
    if not valid or env not in {"dev", "prod"}:
        raise Blocked("database_target_mismatch")
    if (env == "dev" and expected_ref not in DEV_PROJECT_REFS) or (
            env == "prod" and (expected_ref != PROD_PROJECT_REF or not production_ack)):
        raise Blocked("explicit_environment_required")


def reserve_private(path: Path):
    """Create once outside Git, refusing symlinks and unsafe output parents."""
    path = path.absolute()
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise Blocked("unsafe_output_path")
    if any((p / ".git").exists() for p in path.parents):
        raise Blocked("output_must_be_outside_git")
    if not path.parent.is_dir():
        raise Blocked("output_parent_missing")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "w+")


def write_private(stream, value):
    stream.seek(0)
    json.dump(value, stream, sort_keys=True, ensure_ascii=False)
    stream.write("\n")
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())



def approve_live_template(approved, checksum, *, project_ref, code_sha):
    # Plan approval authorizes the all-user algorithm/code, not a stale population
    # snapshot. Start seals the then-current complete prefix privately.
    if (not isinstance(approved, dict) or approved.get("format") != FORMAT
            or digest(approved) != checksum or approved.get("blockers")
            or approved.get("project_ref") != project_ref or approved.get("code_sha") != code_sha):
        raise Blocked("approved_live_plan_changed")


def require_code(expected):
    from app.config import settings
    if not promotion.valid_sha(expected) or settings.git_sha != expected:
        raise Blocked("running_code_sha_mismatch")


def private_env(args):
    value = getattr(args, "env_file", None)
    if value is not None:
        value = Path(value)
        if (not value.is_absolute() or value.is_symlink() or not value.is_file()
                or value.stat().st_mode & 0o077
                or any(p.is_symlink() or (p/".git").exists() for p in value.parents)):
            raise Blocked("private_environment_file_required")
    return str(value) if value else args.env


async def run(args):
    from db.envfile import load_conn
    env = private_env(args)
    dsn = load_conn(env)
    require_target(args.env, dsn, args.expected_project_ref, args.production_ack)
    if args.action not in {"plan", "start", "catch-up", "audit", "activate", "abort"}:
        raise Blocked("live_v2_command_required")
    require_code(args.expected_code_sha)
    if args.action == "start" and (not args.plan or not args.plan_sha256):
        raise Blocked("approved_live_plan_required")
    if args.action not in {"plan", "start"} and not args.preparation_id:
        raise Blocked("preparation_id_required")
    with reserve_private(args.output) as output:
        return await _run(args, dsn, output)


async def _run(args, dsn, output):
    import time
    import uuid
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    url = re.sub(r"^postgres(?:ql)?(?:\+asyncpg)?://", "postgresql+asyncpg://", dsn)
    engine = create_async_engine(url, echo=False, hide_parameters=True, pool_size=1,
        max_overflow=0, connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0})
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        identity = uuid.UUID(str(args.preparation_id)) if args.preparation_id else None
        if args.action in {"plan", "start"}:
            async with maker() as session:
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                plans, blockers, _, excluded = await promotion.collect(session)
                manifest = promotion.live_manifest(plans, excluded,
                    project_ref=args.expected_project_ref, code_sha=args.expected_code_sha)
                report = promotion.public_report(manifest, blockers=blockers)
                await session.rollback()
            write_private(output, report)
            if blockers:
                return 1
            if args.action == "start":
                approved = json.loads(args.plan.read_text())
                approve_live_template(approved, args.plan_sha256,
                    project_ref=args.expected_project_ref, code_sha=args.expected_code_sha)
                async with maker() as session:
                    # Source recheck and registry insertion share one snapshot.
                    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
                    # LOCK takes no MVCC snapshot. The subsequent consistent
                    # snapshot cannot miss a concurrent start/registry transition.
                    await session.execute(text("LOCK TABLE retention_preparations IN SHARE ROW EXCLUSIVE MODE NOWAIT"))
                    fresh, errors, _, fresh_excluded = await promotion.collect(session)
                    if errors:
                        raise Blocked("current_source_blocked")
                    identity, manifest = await promotion.new_preparation(session, plans=fresh,
                        excluded=fresh_excluded, project_ref=args.expected_project_ref,
                        code_sha=args.expected_code_sha)
                    await session.commit()
                # Persist resumable typed identity BEFORE per-user commits.
                report = promotion.public_report(manifest, preparation_id=identity)
                write_private(output, report)
                report = await promotion.prepare_all(maker, identity)
        elif args.action == "catch-up":
            async with maker() as session:
                row = await promotion.registry(session, identity)
                if row["code_sha"] != args.expected_code_sha or row["manifest_json"]["project_ref"] != args.expected_project_ref:
                    raise Blocked("preparation_target_changed")
            report = await promotion.prepare_all(maker, identity, catch_up=True)
        elif args.action == "abort":
            async with maker() as session:
                row = await promotion.registry(session, identity)
                if row["code_sha"] != args.expected_code_sha or row["manifest_json"]["project_ref"] != args.expected_project_ref:
                    raise Blocked("preparation_target_changed")
                report = await promotion.abort(session, identity)
                await session.commit()
        else:
            if args.action == "activate":
                # Private-only metadata is sealed before a fresh source audit.
                # Already-active retries keep their existing idempotent path.
                async with maker() as session:
                    try:
                        row = await promotion.registry(session, identity)
                        if row["code_sha"] != args.expected_code_sha or row["manifest_json"]["project_ref"] != args.expected_project_ref:
                            raise Blocked("preparation_target_changed")
                        if row["state"] != "active":
                            await promotion.seal_preparation(session, identity)
                            await session.commit()
                        else:
                            await session.rollback()
                    except BaseException:
                        await session.rollback()
                        raise
            async with maker() as session:
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                row = await promotion.registry(session, identity)
                if row["code_sha"] != args.expected_code_sha or row["manifest_json"]["project_ref"] != args.expected_project_ref:
                    raise Blocked("preparation_target_changed")
                already_active = args.action == "activate" and row["state"] == "active"
                if already_active:
                    current = await session.scalar(text("SELECT value FROM app_config WHERE key='context_retention_active_preparation'"))
                    if current != str(identity):
                        raise Blocked("active_preparation_pointer_conflict")
                    report = promotion.public_report(row["manifest_json"], preparation_id=identity, ready=True)
                    token = None
                else:
                    token = await promotion.audit_preparation(session, identity)
                    report = token.report
                await session.rollback()
            if args.action == "activate" and token is not None:
                original_created = token.created_monotonic
                for attempt in range(3):
                    async with maker() as session:
                        try:
                            await session.execute(text('SELECT 1'))  # Connection/BEGIN only; no publication locks.
                            from app.services.retention_activation_db import prepare_envelope
                            prepared = await prepare_envelope(session, token)  # Bound metadata only; no public locks.
                            deadline = time.monotonic()+1
                            async with asyncio.timeout_at(deadline):
                                report = await promotion.activate(session, token, deadline=deadline, prepared=prepared)
                                await session.commit()
                            break
                        except promotion.AuditRefreshRequired as changed:
                            await session.rollback()
                            if attempt == 2 or time.monotonic()-original_created >= 60:
                                raise
                            subjects = changed.subjects
                        except BaseException:
                            await session.rollback()
                            raise
                    # No publication transaction/connection is retained while
                    # rechecking the exact changed subjects, never the full population.
                    token = await promotion.refresh_audit_subjects(maker, token, subjects)
        write_private(output, report)
        print(json.dumps({"action": args.action, "ready": report["ready"],
                          "preparation_id": report.get("preparation_id"),
                          "report_sha256": digest(report)}, sort_keys=True))
        return 0
    finally:
        await engine.dispose()


def main():
    import uuid
    from db.envfile import bootstrap_script_environment
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("plan", "start", "catch-up", "audit", "activate", "abort"), default="plan")
    parser.add_argument("--env", choices=("dev", "prod"), default="dev")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--expected-project-ref", required=True)
    parser.add_argument("--expected-code-sha", required=True)
    parser.add_argument("--production-ack", action="store_true")
    parser.add_argument("--preparation-id", type=uuid.UUID)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        bootstrap_script_environment(["--env", private_env(args)])
        return asyncio.run(run(args))
    except Exception as exc:
        print(json.dumps({"action": args.action, "blocked": str(exc) if isinstance(exc, Blocked)
                          else type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
