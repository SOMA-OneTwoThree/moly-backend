"""Reviewed fleet receipt -> atomic chat encryption and guard adoption.

This is an execution tool, not an authorization service. The deployment workflow
must construct its private receipts only after its real checks; callers cannot
substitute an arbitrary user-supplied receipt. No source/history data is read.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402
from app.config import settings  # noqa: E402
from app.core.db import get_sessionmaker, get_engine  # noqa: E402
from app.core.content_crypto import initialize_content_crypto  # noqa: E402
from app.services.chat_content_encryption import parse_mode, STATE_KEY  # noqa: E402
from scripts.check_chat_content_crypto import check  # noqa: E402

PROJECTS = {'development': 'wywzjslvxwttxkecbyis', 'production': 'qkgjlgzsharnilxnkytd'}
GUARD = {'crisis_guard_enabled': True, 'crisis_guard_policy_version': 'crisis-v2',
         'crisis_guard_timeout_s': 2, 'crisis_guard_history_messages': 6}
CONFIG_KEYS = tuple(sorted((*GUARD, STATE_KEY)))
GATE_NAMES = frozenset({'schema', 'daily_report', 'health', 'rollback_reader'})
FORMAT = 'chat-content-release-v1'


class ReleaseBlocked(ValueError):
    pass


def require_target(environment, expected_project_ref, dsn):
    try:
        u = urlsplit(dsn)
        valid = (environment in PROJECTS and expected_project_ref == PROJECTS[environment]
            and u.scheme in {'postgres', 'postgresql', 'postgresql+asyncpg'}
            and not u.query and not u.fragment and u.path == '/postgres'
            and u.port in {None, 5432, 6543})
        host, owner = u.hostname or '', unquote(u.username or '')
        direct = host == f'db.{expected_project_ref}.supabase.co' and owner == 'postgres'
        pool = host.endswith('.pooler.supabase.com') and owner == f'postgres.{expected_project_ref}'
        if not valid or not (direct or pool):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ReleaseBlocked('approved_database_target_required') from None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def validate_snapshot(value):
    if not isinstance(value, dict) or set(value) != set(CONFIG_KEYS):
        raise ReleaseBlocked('config_snapshot_invalid')
    for key, row in value.items():
        if (not isinstance(row, dict) or set(row) != {'exists', 'value'}
                or type(row['exists']) is not bool or (not row['exists'] and row['value'] is not None)):
            raise ReleaseBlocked('config_snapshot_invalid')
        if not row['exists']:
            if key == STATE_KEY:
                raise ReleaseBlocked('encryption_state_missing')
            continue
        v = row['value']
        if key == STATE_KEY:
            parse_mode(v)
        elif key == 'crisis_guard_enabled' and type(v) is not bool:
            raise ReleaseBlocked('guard_config_invalid')
        elif key == 'crisis_guard_policy_version' and (not isinstance(v, str) or not 1 <= len(v) <= 64):
            raise ReleaseBlocked('guard_config_invalid')
        elif key == 'crisis_guard_timeout_s' and (type(v) not in {int, float} or not math.isfinite(v) or not 0.01 <= v <= 2):
            raise ReleaseBlocked('guard_config_invalid')
        elif key == 'crisis_guard_history_messages' and (type(v) is not int or not 1 <= v <= 6):
            raise ReleaseBlocked('guard_config_invalid')



def same_runtime(proof, reference):
    return (isinstance(proof, dict) and set(proof) == set(reference)
        and type(proof.get('started')) is bool
        and type(proof.get('format_version')) is int and proof['format_version'] == 1
        and type(proof.get('active_key_id')) is int and 1 <= proof['active_key_id'] <= 65535
        and isinstance(proof.get('key_fingerprints'), dict)
        and all(isinstance(k, str) and re.fullmatch(r'[1-9][0-9]{0,4}', k) and int(k) <= 65535
            and isinstance(v, str) and re.fullmatch(r'[0-9a-f]{64}', v)
            for k, v in proof['key_fingerprints'].items())
        and any(str(proof['active_key_id']) == k for k in proof['key_fingerprints'])
        and all(proof[k] == v for k, v in reference.items() if k != 'started'))

def validate_release(value, *, environment, project_ref, release_sha, release_id, instance_ids, worker_instance_id, current, expected_rescue_sha):
    expected_keys = {'format', 'environment', 'project_ref', 'release_sha', 'release_id',
                     'instances', 'worker', 'gates', 'config_snapshot'}
    if (not isinstance(value, dict) or set(value) != expected_keys or value['format'] != FORMAT
            or not re.fullmatch(r'[0-9a-f]{40}', release_sha)
            or not re.fullmatch(r'[0-9]{1,24}', release_id)
            or environment not in PROJECTS
            or len(instance_ids) != (2 if environment == 'production' else 1)
            or len(set(instance_ids)) != len(instance_ids)
            or any(not re.fullmatch(r'i-(?:[0-9a-f]{8}|[0-9a-f]{17})', i) for i in instance_ids)
            or worker_instance_id not in instance_ids):
        raise ReleaseBlocked('release_receipt_invalid')
    identity = {'environment': environment, 'project_ref': project_ref,
                'release_sha': release_sha, 'release_id': release_id}
    if any(value[key] != expected for key, expected in identity.items()):
        raise ReleaseBlocked('release_receipt_mismatch')
    validate_snapshot(value['config_snapshot'])
    instances = value['instances']
    if (not isinstance(instances, list) or len(instances) != len(instance_ids)
            or any(not isinstance(item, dict) or set(item) != {'instance_id', 'api', 'consumer'} for item in instances)
            or {item['instance_id'] for item in instances} != set(instance_ids)):
        raise ReleaseBlocked('exact_fleet_required')
    proofs = [item[role] for item in instances for role in ('api', 'consumer')]
    worker = value['worker']
    if (not isinstance(worker, dict) or set(worker) != {'instance_id', 'proof', 'old_tick_drained', 'timer_restored'}
            or worker['instance_id'] != worker_instance_id
            or worker['old_tick_drained'] is not True or worker['timer_restored'] is not True):
        raise ReleaseBlocked('worker_proof_required')
    proofs.append(worker['proof'])
    for proof in proofs:
        if not same_runtime(proof, current):
            raise ReleaseBlocked('runtime_key_or_policy_mismatch')
    gates = value['gates']
    if not isinstance(gates, dict) or set(gates) != GATE_NAMES:
        raise ReleaseBlocked('trusted_release_gates_required')
    for name, gate in gates.items():
        common = {**identity, 'origin': 'trusted-deploy-workflow', 'status': 'passed'}
        if name != 'rollback_reader':
            if gate != common:
                raise ReleaseBlocked('trusted_release_gate_failed')
            continue
        if (not isinstance(expected_rescue_sha, str)
                or not re.fullmatch(r'[0-9a-f]{40}', expected_rescue_sha)
                or expected_rescue_sha == release_sha or not isinstance(gate, dict)
                or set(gate) != {*common, 'rescue_sha', 'instances', 'worker'}
                or any(gate[k] != v for k, v in common.items())
                or gate['rescue_sha'] != expected_rescue_sha):
            raise ReleaseBlocked('rollback_reader_required')
        rescue_instances, rescue_worker = gate['instances'], gate['worker']
        if (not isinstance(rescue_instances, list) or len(rescue_instances) != len(instance_ids)
                or any(not isinstance(i, dict) or set(i) != {'instance_id', 'api', 'consumer'} for i in rescue_instances)
                or {i['instance_id'] for i in rescue_instances} != set(instance_ids)
                or not isinstance(rescue_worker, dict) or set(rescue_worker) != {'instance_id', 'proof'}
                or rescue_worker['instance_id'] != worker_instance_id):
            raise ReleaseBlocked('rollback_reader_required')
        rescue_current = {**current, 'version': expected_rescue_sha}
        for proof in [*(i[role] for i in rescue_instances for role in ('api', 'consumer')), rescue_worker['proof']]:
            if not same_runtime(proof, rescue_current):
                raise ReleaseBlocked('rollback_reader_mismatch')



def snapshot_rows(rows):
    values = {row[0]: row[1] for row in rows}
    return {key: {'exists': key in values, 'value': values.get(key)} for key in CONFIG_KEYS}


def policy_equivalent(expected, actual):
    if expected == actual:
        return True
    # A compatible earlier attempt may have committed while its acknowledgement
    # was lost. Only one-way adoption and the exact canonical guard values match.
    if expected['exists'] and actual['exists']:
        a, b = expected['value'], actual['value']
        if isinstance(a, dict) and isinstance(b, dict) and a.get('started') is False and b.get('started') is True:
            ignored = {'started', 'activated_at', 'release_sha'}
            return {k: v for k, v in a.items() if k not in ignored} == {k: v for k, v in b.items() if k not in ignored}
    return False


async def activate_release(session, *, receipt, current, expected_sha):
    # Close the missing-row gap without a new table/index: config writers wait
    # for this bounded transaction; regular config SELECTs remain compatible.
    await session.execute(text("SELECT set_config('statement_timeout','1000ms',true),set_config('lock_timeout','1ms',true)"))
    await session.execute(text('LOCK TABLE public.app_config IN SHARE ROW EXCLUSIVE MODE NOWAIT'))
    rows = (await session.execute(text('SELECT key,value FROM public.app_config WHERE key=ANY(:keys) ORDER BY key FOR UPDATE NOWAIT'),
                                  {'keys': list(CONFIG_KEYS)})).all()
    actual, expected = snapshot_rows(rows), receipt['config_snapshot']
    validate_snapshot(actual)
    if not policy_equivalent(expected[STATE_KEY], actual[STATE_KEY]):
        raise ReleaseBlocked('encryption_state_changed')
    current_mode = parse_mode(actual[STATE_KEY]['value'])
    if (current_mode.active_key_id != current['active_key_id']
            or set(current_mode.required_key_ids) != {int(key) for key in current['key_fingerprints']}
            or current_mode.format_version != current['format_version']):
        raise ReleaseBlocked('encryption_state_changed')
    for key, target in GUARD.items():
        if actual[key] != expected[key] and actual[key] != {'exists': True, 'value': target}:
            raise ReleaseBlocked('guard_config_changed')
    initialize_content_crypto(required_key_ids=current_mode.required_key_ids)
    updates = [{'key': key, 'value': target} for key, target in GUARD.items()
               if actual[key] != {'exists': True, 'value': target}]
    if not current_mode.started:
        adopted = {**actual[STATE_KEY]['value'], 'started': True,
                   'activated_at': datetime.now(timezone.utc).isoformat(), 'release_sha': expected_sha}
        updates.append({'key': STATE_KEY, 'value': adopted})
    if updates:
        await session.execute(text("INSERT INTO public.app_config(key,value) "
            "SELECT key,value FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS data(key text,value jsonb) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()"),
            {'rows': json.dumps(updates, separators=(',', ':'))})
    return parse_mode({**actual[STATE_KEY]['value'], 'started': True})



async def run(*, environment, expected_project_ref, expected_sha, release_id, instance_ids, worker_instance_id, receipt, expected_rescue_sha):
    if settings.environment != environment:
        raise ReleaseBlocked('runtime_environment_mismatch')
    require_target(environment, expected_project_ref, settings.supabase_db_connection_string)
    current = await check(environment, expected_sha)
    validate_release(receipt, environment=environment, project_ref=expected_project_ref,
        release_sha=expected_sha, release_id=release_id, instance_ids=instance_ids,
        worker_instance_id=worker_instance_id, current=current, expected_rescue_sha=expected_rescue_sha)
    for attempt in range(3):
        try:
            async with get_sessionmaker()() as session:
                async with asyncio.timeout(1):
                    mode = await activate_release(session, receipt=receipt, current=current, expected_sha=expected_sha)
                    await session.commit()
            return {'format': FORMAT, 'environment': environment, 'version': expected_sha,
                    'started': mode.started, 'guard_enabled': True, 'release_id': release_id}
        except DBAPIError as error:
            if getattr(error.orig, 'sqlstate', None) != '55P03' or attempt == 2:
                raise
            await asyncio.sleep(0.1 * (attempt + 1))


async def main(args):
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ReleaseBlocked('release_receipt_too_large')
        result = await run(environment=args.environment, expected_project_ref=args.expected_project_ref,
            expected_sha=args.expected_sha, release_id=args.release_id, instance_ids=args.instance_id,
            worker_instance_id=args.worker_instance_id, receipt=json.loads(raw), expected_rescue_sha=args.expected_rescue_sha)
        print(json.dumps(result, sort_keys=True))
    finally:
        await get_engine().dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', required=True, choices=tuple(PROJECTS))
    parser.add_argument('--expected-project-ref', required=True)
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--release-id', required=True)
    parser.add_argument('--expected-rescue-sha', required=True)
    parser.add_argument('--instance-id', action='append', required=True)
    parser.add_argument('--worker-instance-id', required=True)
    try:
        asyncio.run(main(parser.parse_args()))
    except Exception as error:
        print(json.dumps({'status': 'blocked', 'error_type': type(error).__name__}))
        raise SystemExit(1) from None
