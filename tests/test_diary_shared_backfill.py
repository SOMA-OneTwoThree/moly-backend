"""Backfill approval boundaries; no database or provider calls."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

import pytest

from scripts import diary_shared_embeddings as tool


def reviewed(action='link', project='wywzjslvxwttxkecbyis'):
    return {'version': 1, 'action': action, 'project': project,
            'cutoff': datetime.now(timezone.utc).isoformat(), 'entries': [{
                'source_hash': 'a' * 64, 'embedding_model': 'text-embedding-3-small',
                'index_version': 'diary-recall-v1', 'search_digest': 'b' * 64,
                'vector_digest': 'c' * 64, 'candidate_count': 5}]}


def write_review(tmp_path, plan):
    path = tmp_path / 'plan.json'
    raw = tool.encode_plan(plan)
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def test_plan_is_hash_bound_to_exact_bytes_and_operation(tmp_path):
    plan = reviewed()
    path, digest = write_review(tmp_path, plan)
    assert tool.load_plan(path, digest, 'link', plan['project']) == plan
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ValueError, match='checksum'):
        tool.load_plan(path, digest, 'link', plan['project'])


@pytest.mark.parametrize('change', ['operation', 'project', 'no_hash', 'invalid_key', 'naive_time'])
def test_plan_cannot_cross_approval_boundaries(tmp_path, change):
    plan = reviewed()
    if change == 'invalid_key':
        plan['entries'][0]['vector_digest'] = 'not-an-approved-fingerprint'
    if change == 'naive_time':
        plan['cutoff'] = '2026-10-28T00:00:00'
    path, digest = write_review(tmp_path, plan)
    with pytest.raises(ValueError):
        tool.load_plan(path, None if change == 'no_hash' else digest,
                       'prune' if change == 'operation' else 'link',
                       'other-project' if change == 'project' else plan['project'])


def test_unknown_database_and_implicit_production_writes_are_refused():
    with pytest.raises((ValueError, SystemExit)):
        tool.guard_target('.env.prod', 'postgresql://postgres:password@localhost/db',
                          commit=True, allow_prod=True)
    with pytest.raises(SystemExit):
        tool.guard_target('.env.prod',
                          'postgresql://postgres.qkgjlgzsharnilxnkytd:password@pooler/db',
                          commit=True, allow_prod=False)
    with pytest.raises(ValueError):
        tool.guard_target('.env',
                          'postgresql://postgres.wywzjslvxwttxkecbyis:password@pooler/db',
                          commit=True, allow_prod=True)


def test_replanning_order_and_hash_are_deterministic():
    plan = reviewed()
    assert tool.encode_plan(plan) == tool.encode_plan(json.loads(tool.encode_plan(plan)))


class ModeConnection:
    def __init__(self, value):
        self.value = value

    async def fetchval(self, query, *args):
        return self.value


@pytest.mark.parametrize('value,expected', [(None, 'private'), ('"dual"', 'dual'),
                                            ('"shared"', 'shared'), ('"private"', 'private')])
async def test_mode_requires_supported_exact_value(value, expected):
    assert await tool.read_mode(ModeConnection(value)) == expected


@pytest.mark.parametrize('value', ['"true"', 'true', '{}', '"SHARED"'])
async def test_unknown_mode_never_authorizes_data_changes(value):
    with pytest.raises((ValueError, TypeError)):
        await tool.read_mode(ModeConnection(value))


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class BatchConnection:
    def transaction(self):
        return Transaction()

    async def execute(self, *args):
        return None

    async def fetchval(self, query, *args):
        if 'app_config' in query:
            return '"dual"'
        return 0

    async def fetch(self, query, *args):
        if args[2] == 'last-key':
            return [{'user_id': uuid.uuid4(), 'diary_id': uuid.uuid4()}]
        return []


async def test_resuming_after_completed_keys_does_not_exhaust_batch_budget(monkeypatch):
    plan = reviewed()
    prototype = plan['entries'][0]
    plan['entries'] = [{**prototype, 'source_hash': f'completed-{i}'} for i in range(20)]
    plan['entries'].append({**prototype, 'source_hash': 'last-key'})

    async def seeded(*args):
        return True

    async def changed(*args):
        return True

    monkeypatch.setattr(tool, 'seed_canonical', seeded)
    monkeypatch.setattr(tool, 'mutate_one', changed)
    result = await tool.apply_plan(BatchConnection(), plan, welcome=([], []), batch=50, max_batches=1)
    assert result['changed'] == 1
    assert result['batches'] == 1


async def test_source_changed_during_lock_wait_is_skipped_without_update():
    class Connection:
        def __init__(self):
            self.queries = []

        async def execute(self, query, *args):
            self.queries.append(query)

        async def fetchrow(self, query, *args):
            self.queries.append(query)
            return None  # epoch/generation/updated_at fence rejected the fresh row

    conn = Connection()
    candidate = {'user_id': uuid.uuid4(), 'diary_id': uuid.uuid4(), 'privacy_epoch': 1,
                 'suppression_generation': 1, 'updated_at': datetime.now(timezone.utc)}
    assert await tool.mutate_one(conn, 'prune', reviewed()['entries'][0], candidate, ([], [])) is False
    assert not any(query.lstrip().startswith('UPDATE') for query in conn.queries)


def test_migration_is_additive_and_contains_no_data_operation():
    sql = (Path(__file__).resolve().parents[1] / 'db/changes/diary_shared_embeddings.sql').read_text()
    statements = [line.strip().upper() for line in sql.splitlines()
                  if line.strip() and not line.lstrip().startswith('--')]
    assert not any(line.startswith(('DELETE ', 'UPDATE ', 'TRUNCATE ', 'INSERT ')) for line in statements)
    assert 'IF NOT EXISTS' not in sql


def test_restore_preserves_recall_visibility_of_older_generations():
    current_generation_filter = 'rd.suppression_generation=COALESCE(c.memory_generation,0)'
    assert current_generation_filter in tool.base_sql('link')
    assert current_generation_filter in tool.base_sql('prune')
    assert current_generation_filter not in tool.base_sql('restore')
    assert 'd.published_at<=clock_timestamp()' not in tool.base_sql('restore')
    assert 'd.published_at IS NOT NULL' in tool.base_sql('restore')
    # Restoration does not weaken the captured row generation/epoch CAS.
    assert 'rd.suppression_generation' in tool.base_sql('restore')
    assert "b.state='active'" in tool.base_sql('restore')
    assert "rd.source_hash=encode(digest" in tool.base_sql('restore')
