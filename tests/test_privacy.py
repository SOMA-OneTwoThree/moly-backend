"""계정 삭제 장벽과 late worker publish의 교차 계층 계약."""
from __future__ import annotations

import uuid

import pytest

from app.core.errors import AppError
from app.services import jobs, privacy


UID = uuid.UUID("11111111-1111-1111-1111-111111111111")
OPERATION_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


class _Result:
    def __init__(self, row=None):
        self._row = row

    def first(self):
        return self._row


class _Session:
    def __init__(self, *, scalars=(), rows=()):
        self.scalars = list(scalars)
        self.rows = list(rows)
        self.calls = []

    async def scalar(self, stmt, params=None):
        self.calls.append((stmt, params))
        return self.scalars.pop(0)

    async def execute(self, stmt, params=None):
        self.calls.append((stmt, params))
        row = self.rows.pop(0) if self.rows else None
        return _Result(row)


async def test_subject_barrier_blocks_every_serving_path() -> None:
    with pytest.raises(AppError) as caught:
        await privacy.ensure_subject_active(_Session(scalars=[True]), UID)
    assert caught.value.code == "ACCOUNT_DELETING"


async def test_begin_deletion_redacts_replay_and_job_copies_before_account_cascade() -> None:
    session = _Session(scalars=[17], rows=[(2, 3, 5)])
    counts = await privacy.begin_subject_deletion(
        session, user_id=UID, operation_id=OPERATION_ID
    )
    assert counts == (2, 3, 5)
    statements = [call[0] for call in session.calls]
    assert privacy._BEGIN in statements
    assert privacy._REDACT in statements
    assert privacy._LEDGER in statements


async def test_mark_deleted_is_operation_fenced() -> None:
    assert not await privacy.mark_subject_deleted(
        _Session(scalars=[None]), user_id=UID, operation_id=OPERATION_ID
    )
    session = _Session(scalars=[17])
    assert await privacy.mark_subject_deleted(
        session, user_id=UID, operation_id=OPERATION_ID
    )
    assert any(stmt is privacy._LEDGER for stmt, _ in session.calls)


def test_success_finalize_checks_barrier_before_domain_apply() -> None:
    sql = str(jobs._SUCCESS_SQL).lower()
    assert "privacy_subject_barriers" in sql
    assert "not exists" in sql


def test_retention_scrubs_payload_bodies_but_keeps_terminal_rows() -> None:
    sql = str(jobs._SCRUB_RETENTION_SQL).lower()
    assert "update async_jobs" in sql
    assert "payload='{}'::jsonb" in sql
    assert "delete from async_jobs" not in sql
    assert "update idempotency_keys" in sql
