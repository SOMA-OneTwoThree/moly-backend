import uuid

from app.services import memory_forget as forget
from app.services import memory_repo

UID = uuid.UUID("11111111-1111-1111-1111-111111111111")
FACT = uuid.UUID("22222222-2222-2222-2222-222222222222")


class Intent:
    def __init__(self, kind, target_fact_ids=(), value=None):
        self.kind = kind
        self.target_fact_ids = tuple(target_fact_ids)
        self.value = value


def test_classify_only_accepts_typed_forget_targets():
    assert forget.classify(Intent("forget", (FACT,))) == forget.ForgetRequest(
        scope="fact", fact_ids=(FACT,)
    )
    assert forget.classify(Intent("pin", (FACT,))) is None
    assert forget.classify(Intent("forget", value="자유문")) is None


def test_fact_updates_remove_vector_in_the_same_statement():
    for stmt in (
        forget._FORGET_FACT_SQL,
        forget._FORGET_PREDICATE_SQL,
        forget._FORGET_ALL_FACTS_SQL,
    ):
        sql = str(stmt).replace(" ", "").replace("\n", "").lower()
        assert "status='forgotten'" in sql
        assert "embedding=null" in sql
        assert "status='active'" in sql


class Result:
    def __init__(self, row=None, rowcount=0):
        self._row = row
        self.rowcount = rowcount

    def first(self):
        return self._row


class Session:
    def __init__(self):
        self.calls = []

    async def execute(self, stmt, params=None):
        self.calls.append((stmt, params))
        if stmt is forget._LOCK_CONTEXT_SQL:
            return Result((2, 9, 4))  # generation, watermark, revision
        if stmt is forget._BUMP_SQL:
            return Result((3, 5))
        if stmt is forget._DELETE_CHECKPOINTS_SQL:
            return Result(rowcount=1)
        return Result()


async def test_apply_closes_source_then_invalidates_derivatives(monkeypatch):
    order = []

    async def targets(*args, **kwargs):
        return (forget._Target(FACT, "hash", "memory-fact-v1", 3, 4),)

    async def markers(*args, **kwargs):
        order.append("marker")
        return 1

    async def facts(*args, **kwargs):
        order.append("fact")
        return (FACT,)

    async def insights(*args, **kwargs):
        order.append("insight")
        return ()

    async def profiles(*args, **kwargs):
        order.append("profile")
        return ()

    async def enqueue(*args, **kwargs):
        order.append("refresh")

    monkeypatch.setattr(forget, "_resolve_targets", targets)
    monkeypatch.setattr(forget, "_write_markers", markers)
    monkeypatch.setattr(forget, "_forget_facts", facts)
    monkeypatch.setattr(forget, "_invalidate_insights", insights)
    monkeypatch.setattr(forget, "_invalidate_profiles", profiles)
    monkeypatch.setattr(memory_repo, "enqueue_profile_refresh", enqueue)

    session = Session()
    result = await forget.apply(
        session,
        user_id=UID,
        request=forget.ForgetRequest(scope="fact", fact_ids=(FACT,)),
        operation_id=uuid.UUID("33333333-3333-3333-3333-333333333333"),
    )

    assert result.status == forget.RESULT_APPLIED
    assert result.closure == (3, 4)
    assert result.memory_generation == 3
    assert result.deleted_checkpoints == 1
    assert order == ["marker", "fact", "insight", "profile", "refresh"]
    statements = [stmt for stmt, _ in session.calls]
    assert statements.index(forget._INSERT_CLOSURE_SQL) < statements.index(forget._BUMP_SQL)


async def test_missing_context_writes_nothing():
    class Empty(Session):
        async def execute(self, stmt, params=None):
            self.calls.append((stmt, params))
            return Result()

    session = Empty()
    result = await forget.apply(
        session, user_id=UID, request=forget.ForgetRequest(scope="all")
    )
    assert result.status == forget.RESULT_NOTHING_MATCHED
    assert len(session.calls) == 1


def test_no_external_vector_delete_job_remains():
    source = __import__("inspect").getsource(forget)
    assert "memory_vector_delete" not in source
    assert "delete_all" not in source
