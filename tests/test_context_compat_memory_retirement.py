"""Semantic retirement and opaque historical summary share one publication TX."""
from __future__ import annotations

import uuid

import pytest

from app.services import mem0_registry_repo as repo
from app.services.mem0_consolidation import Transition


class Result:
    def __init__(self, rows=()):
        self.rows = rows

    def first(self):
        return next(iter(self.rows), None)


class Session:
    def __init__(self, *, baseline=True, applied=True, invalidation=True, revision=2):
        self.baseline = baseline
        self.applied = applied
        self.invalidation = invalidation
        self.revision = revision
        self.calls = []

    async def execute(self, statement, params):
        sql = str(statement)
        self.calls.append((sql, params))
        if sql == str(repo._CHECK_REVISION):
            return Result([(self.revision, 0)])
        if sql == str(repo._LOCK_COMPAT_SUMMARY):
            return Result([(params['user_id'],)] if self.baseline else [])
        if sql == str(repo._APPLY):
            return Result([(params['id'],)] if self.applied else [])
        if sql == str(repo._INVALIDATE_COMPAT_SUMMARY):
            return Result([(params['user_id'],)] if self.invalidation else [])
        raise AssertionError('unexpected statement')


async def apply(session, *statuses):
    return await repo.apply_transitions(session, uuid.uuid4(), [
        Transition(registry_id=uuid.uuid4(), semantic_status=status)
        for status in statuses
    ], expected_revision=2, classification_version='review')


@pytest.mark.parametrize('status', ['superseded', 'excluded'])
async def test_retirement_locks_context_before_registry_and_invalidates_same_tx(status):
    session = Session()
    assert await apply(session, status) == 1
    assert [sql for sql, _ in session.calls] == [str(statement) for statement in (
        repo._CHECK_REVISION, repo._LOCK_COMPAT_SUMMARY, repo._APPLY,
        repo._INVALIDATE_COMPAT_SUMMARY,
    )]


@pytest.mark.parametrize('status', ['active', 'ambiguous', 'pending', 'duplicate'])
async def test_nondestructive_status_has_no_compatibility_queries(status):
    session = Session()
    assert await apply(session, status) == 1
    assert [sql for sql, _ in session.calls] == [str(repo._CHECK_REVISION), str(repo._APPLY)]


async def test_terminal_noop_does_not_invalidate():
    session = Session(applied=False)
    assert await apply(session, 'superseded') == 0
    assert all(sql != str(repo._INVALIDATE_COMPAT_SUMMARY) for sql, _ in session.calls)


async def test_no_baseline_keeps_legacy_transition_behavior():
    session = Session(baseline=False)
    assert await apply(session, 'excluded') == 1
    assert all(sql != str(repo._INVALIDATE_COMPAT_SUMMARY) for sql, _ in session.calls)


async def test_multiple_retirements_record_first_invalidation_only_once():
    session = Session()
    assert await apply(session, 'excluded', 'superseded') == 2
    assert sum(sql == str(repo._INVALIDATE_COMPAT_SUMMARY) for sql, _ in session.calls) == 1


async def test_missing_locked_binding_is_failure_not_unobserved_success():
    session = Session(invalidation=False)
    with pytest.raises(repo.StaleConsolidation):
        await apply(session, 'superseded')


async def test_stale_model_result_does_not_lock_or_change_summary():
    session = Session(revision=3)
    with pytest.raises(repo.StaleConsolidation):
        await apply(session, 'excluded')
    assert len(session.calls) == 1


def test_sql_owner_generation_monotonicity_and_immutable_payload():
    for statement in (repo._LOCK_COMPAT_SUMMARY, repo._INVALIDATE_COMPAT_SUMMARY):
        sql = str(statement)
        assert "user_id=:user_id" in sql
        assert "retention_compat_json->>'user_id'=user_id::text" in sql
        assert "retention_compat_json->'generation'=to_jsonb(memory_generation)" in sql
    assert 'FOR UPDATE' in str(repo._LOCK_COMPAT_SUMMARY)
    update = str(repo._INVALIDATE_COMPAT_SUMMARY)
    assert 'COALESCE(' in update
    assert 'retention_compat_json=' not in update
    assert 'canonical_text' not in update
