"""Missing chain links may be filled without granting existing jobs a new budget."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import checkpoint_repo as repo, context_retention, memory_pipeline


@pytest.fixture
def setup(monkeypatch):
    state = SimpleNamespace(privacy_epoch=3, ingest_through_turn_seq=5, repair_generation=7)
    uid = uuid.uuid4()
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(first=lambda: (uid,))),
                              scalar=AsyncMock(return_value=True))
    for name in ('enabled_now', 'adopted_user', 'privacy_epoch_matches'):
        monkeypatch.setattr(context_retention, name, AsyncMock(return_value=True))
    monkeypatch.setattr(memory_pipeline, 'load', AsyncMock(return_value=state))
    monkeypatch.setattr(memory_pipeline, 'next_ingest_turn', AsyncMock(return_value=9))
    monkeypatch.setattr(memory_pipeline, 'enqueue_ingest', AsyncMock(return_value=uuid.uuid4()))
    return session, uid


async def test_new_link_uses_fresh_cursor_and_repair_axis_not_context_generation(setup):
    session, uid = setup
    assert await repo.ensure_pressure_ingest(session, user_id=uid, generation=2, through_id=400)
    memory_pipeline.enqueue_ingest.assert_awaited_once_with(session, uid, turn_seq=9, cursor=5,
        privacy_epoch=3, generation=7, delay_s=0)
    assert session.scalar.await_args.args[1] == dict(user_id=uid, next_turn=9, through_id=400,
        dedup_key=memory_pipeline.ingest_dedup_key(uid, 5, generation=7),
        resume_prefix=f'resume:{uid}:c5:g7:')


@pytest.mark.parametrize('condition', ['disabled', 'unadopted', 'busy', 'epoch', 'no_source', 'existing'])
async def test_no_new_key_for_disabled_busy_stale_or_existing_work(setup, condition):
    session, uid = setup
    if condition == 'disabled':
        context_retention.enabled_now.return_value = False
    elif condition == 'unadopted':
        context_retention.adopted_user.return_value = False
    elif condition == 'busy':
        session.execute.return_value.first = lambda: None
    elif condition == 'epoch':
        context_retention.privacy_epoch_matches.return_value = False
    elif condition == 'no_source':
        memory_pipeline.next_ingest_turn.return_value = None
    else:
        session.scalar.return_value = False
    assert not await repo.ensure_pressure_ingest(session, user_id=uid, generation=2, through_id=400)
    memory_pipeline.enqueue_ingest.assert_not_awaited()
