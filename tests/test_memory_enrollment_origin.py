"""Origin is a compatibility read boundary, never a backfill completion proof."""
import uuid

import pytest

from app.services import memory_pipeline as pipeline


class Session:
    def __init__(self, result):
        self.result = result
        self.calls = []
    async def execute(self, sql, params):
        self.calls.append((sql, params))
        class Result:
            def first(inner):
                return self.result
        return Result()


async def test_default_enrollment_keeps_the_legacy_statement():
    session = Session((uuid.uuid4(),))
    assert await pipeline.enroll(session,uuid.uuid4())
    assert session.calls[0][0] is pipeline._ENROLL


@pytest.mark.parametrize("status,expected", [("created",True),("existing",False)])
async def test_origin_arguments_are_explicit_and_existing_state_is_not_repositioned(status,expected):
    session = Session((status,))
    uid = uuid.uuid4()
    assert await pipeline.enroll(session,uid,source_after_message_id=500,privacy_epoch=7) is expected
    assert session.calls == [(pipeline._ENROLL_AT_ORIGIN,
        {"user_id":uid,"source_after":500,"privacy_epoch":7})]
    query = str(pipeline._ENROLL_AT_ORIGIN)
    assert "ON CONFLICT(user_id) DO NOTHING" in query
    assert "UPDATE" not in query


async def test_crossed_turn_cannot_silently_skip_a_visible_suffix():
    with pytest.raises(pipeline.EnrollmentOriginError,match="crossed_turn"):
        await pipeline.enroll(Session(("crossed",)),uuid.uuid4(),source_after_message_id=3)


@pytest.mark.parametrize("kwargs", [{"source_after_message_id":True},{"source_after_message_id":-1},
    {"source_after_message_id":"2"},{"privacy_epoch":True},{"privacy_epoch":-1}])
async def test_origin_arguments_are_strict_nonnegative_integers(kwargs):
    session = Session(None)
    with pytest.raises(pipeline.EnrollmentOriginError):
        await pipeline.enroll(session,uuid.uuid4(),**kwargs)
    assert session.calls == []
