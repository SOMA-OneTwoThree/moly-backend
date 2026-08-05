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
        await privacy.ensure_subject_active(
            _Session(rows=[("deleting", 1, OPERATION_ID)]), UID
        )
    assert caught.value.code == "ACCOUNT_DELETING"


async def test_deleted_subject_also_blocked() -> None:
    with pytest.raises(AppError):
        await privacy.ensure_subject_active(_Session(rows=[("deleted", 1, OPERATION_ID)]), UID)


async def test_active_barrier_does_not_block_serving() -> None:
    """`active` 행이 깔린 뒤에도 대화가 되어야 한다 — 예전 '행 존재=차단'이면 전 사용자가 막힌다."""
    await privacy.ensure_subject_active(_Session(rows=[("active", 0, None)]), UID)


async def test_missing_barrier_does_not_block_serving_during_backfill() -> None:
    await privacy.ensure_subject_active(_Session(rows=[]), UID)


# ─────────────────────────────────────────────────────────────
# authorize_job 상태표(12.3절)
# ─────────────────────────────────────────────────────────────
def _barrier(status, epoch=0, operation_id=None):
    return privacy.BarrierState(status=status, epoch=epoch, operation_id=operation_id)


def test_active_allows_normal_job_and_rejects_coordinator() -> None:
    active = _barrier(privacy.STATUS_ACTIVE)
    assert privacy.authorize_job(active, job_type="memory_extract").allowed is True
    denied = privacy.authorize_job(active, job_type="privacy_delete_coordinator")
    assert denied.allowed is False and denied.reason == "coordinator_on_active"


def test_deleting_allows_only_allowlisted_privacy_jobs() -> None:
    """일반 job을 전부 막되 삭제 continuation은 통과해야 삭제가 진행된다."""
    b = _barrier(privacy.STATUS_DELETING, epoch=3, operation_id=OPERATION_ID)
    assert privacy.authorize_job(b, job_type="memory_extract").reason == "subject_deleting"
    ok = privacy.authorize_job(
        b, job_type="privacy_provider_cleanup", payload_epoch=3, operation_id=OPERATION_ID
    )
    assert ok.allowed is True


def test_deleting_rejects_stale_epoch_and_foreign_operation() -> None:
    b = _barrier(privacy.STATUS_DELETING, epoch=3, operation_id=OPERATION_ID)
    stale = privacy.authorize_job(
        b, job_type="privacy_verify_residual", payload_epoch=2, operation_id=OPERATION_ID
    )
    assert stale.reason == "epoch_mismatch"
    foreign = privacy.authorize_job(
        b, job_type="privacy_verify_residual", payload_epoch=3, operation_id=uuid.uuid4()
    )
    assert foreign.reason == "operation_mismatch"


def test_deleted_rejects_everything() -> None:
    b = _barrier(privacy.STATUS_DELETED, epoch=1, operation_id=OPERATION_ID)
    for jt in ("memory_extract", "privacy_provider_cleanup"):
        assert privacy.authorize_job(b, job_type=jt).allowed is False


def test_stale_epoch_job_rejected_on_active_barrier() -> None:
    """삭제 사이클을 지나온 옛 세대 잡이 새 epoch에서 되살아나지 않는다."""
    b = _barrier(privacy.STATUS_ACTIVE, epoch=2)
    assert privacy.authorize_job(b, job_type="memory_extract", payload_epoch=1).reason == (
        "epoch_mismatch"
    )


def test_missing_barrier_is_compat_allowed_but_enforced_denied() -> None:
    b = _barrier(privacy.STATUS_MISSING)
    assert privacy.authorize_job(b, job_type="memory_extract").allowed is True
    denied = privacy.authorize_job(
        b, job_type="memory_extract", mode=privacy.MODE_ENFORCED
    )
    assert denied.allowed is False and denied.reason == "barrier_missing"


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
