"""forget(W10) — 기본 off에서 아무것도 쓰지 않는지 / 켰을 때 7단계 순서·terminal 불가역.

실 DB에 붙지 않는다. `_FakeSession`이 문장을 SQL 텍스트로 식별해 캔 결과를 돌려주고 실행 순서를
기록한다(`test_memory_repo`와 같은 방식). 시뮬레이터만으로는 SQL을 바꿔도 통과하므로, 문장 자체의
불변식(출발 상태 WHERE·되살리는 UPDATE 부재·marker 삭제 부재)은 소스 구조로 따로 못 박는다.
"""
from __future__ import annotations

import pathlib
import re
import uuid

import pytest

from app.config import settings
from app.services import jobs, memory_forget as forget, memory_repo

_UID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_FACT = uuid.UUID("aaaaaaaa-1111-1111-1111-111111111111")
_FACT2 = uuid.UUID("aaaaaaaa-2222-2222-2222-222222222222")
_INSIGHT = uuid.UUID("cccccccc-3333-3333-3333-333333333333")
_PROFILE = uuid.UUID("dddddddd-4444-4444-4444-444444444444")

_SRC = pathlib.Path(forget.__file__).read_text(encoding="utf-8")


class _Intent:
    """`llm.ControlIntent`와 같은 모양(분류는 덕타이핑이라 llm 모듈에 의존하지 않는다)."""

    def __init__(self, kind: str, target_fact_ids=(), value=None) -> None:
        self.kind = kind
        self.target_fact_ids = tuple(target_fact_ids)
        self.value = value


class _Res:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, **canned) -> None:
        self.calls: list[tuple[str, object]] = []
        self.mode = canned.get("mode", "normalized")
        self.generation = canned.get("generation", 3)
        self.watermark = canned.get("watermark", 12)
        self.revision = canned.get("revision", 5)
        self.targets = canned.get(
            "targets",
            [
                {
                    "id": _FACT,
                    "content_hash": "h1",
                    "normalization_version": "memory-fact-v1",
                    "status": "active",
                    "from_wm": 4,
                    "through_wm": 6,
                    "evidence_n": 2,
                    "mapped_n": 2,
                }
            ],
        )
        self.forgotten = canned.get("forgotten", [(_FACT,)])
        self.insights = canned.get("insights", [(_INSIGHT,)])
        self.profiles = canned.get("profiles", [(_PROFILE,)])

    @property
    def statements(self) -> list[str]:
        return [c[0] for c in self.calls]

    def index_of(self, sql) -> int:
        return self.statements.index(str(sql))

    def executed(self, sql) -> list[object]:
        return [p for s, p in self.calls if s == str(sql)]

    def ran(self, sql) -> bool:
        return str(sql) in self.statements

    async def execute(self, stmt, params=None):
        s = str(stmt)
        self.calls.append((s, params))
        if s == str(forget._LOCK_CONTEXT_SQL):
            if self.mode is None:
                return _Res([])
            return _Res([(self.mode, self.generation, self.watermark, self.revision)])
        if s == str(forget._FACT_TARGETS_SQL):
            return _Res(self.targets)
        if s == str(forget._BUMP_SQL):
            return _Res([(self.generation + 1, self.revision + 1)])
        if s in (
            str(forget._FORGET_FACT_SQL),
            str(forget._FORGET_PREDICATE_SQL),
            str(forget._FORGET_ALL_FACTS_SQL),
        ):
            return _Res(self.forgotten)
        if s in (str(forget._INVALIDATE_INSIGHTS_SQL), str(forget._INVALIDATE_ALL_INSIGHTS_SQL)):
            return _Res(self.insights)
        if s in (str(forget._INVALIDATE_PROFILES_SQL), str(forget._INVALIDATE_ALL_PROFILES_SQL)):
            return _Res(self.profiles)
        if s == str(jobs._ENQUEUE_SQL):
            return _Res([(uuid.uuid4(),)])
        if s in (str(forget._INSERT_CLOSURE_SQL), str(forget._INSERT_MARKER_SQL)):
            return _Res([])
        raise AssertionError(f"시뮬레이터가 모르는 문장: {s[:80]}")


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "memory_forget_enabled", True)


# ─────────────────────────────────────────────────────────────
# 1) 분류 — 플래그와 무관하게 돈다. 해석 못 하는 intent는 실행 대상이 아니다.
# ─────────────────────────────────────────────────────────────
def test_classify_fact_scope():
    req = forget.classify(_Intent("forget", target_fact_ids=(_FACT, _FACT2)))
    assert req == forget.ForgetRequest(scope="fact", fact_ids=(_FACT, _FACT2))


def test_classify_predicate_scope_only_for_registry_predicates():
    assert forget.classify(_Intent("forget", value="residence")).scope == "predicate"
    assert forget.classify(_Intent("forget", value="아무거나")) is None


def test_classify_never_infers_all_scope():
    """"다 잊어줘"류 자유문은 전량 삭제로 승격되지 않는다(범위가 제품 미결)."""
    assert forget.classify(_Intent("forget", value="전부")) is None
    assert forget.classify(_Intent("forget", value="all")) is None


def test_classify_ignores_pin_intent():
    assert forget.classify(_Intent("pin", target_fact_ids=(_FACT,))) is None


def test_classify_all_drops_unresolvable():
    out = forget.classify_all(
        [_Intent("forget", target_fact_ids=(_FACT,)), _Intent("forget", value="자유문"),
         _Intent("pin", value="커피")]
    )
    assert [r.scope for r in out] == ["fact"]


# ─────────────────────────────────────────────────────────────
# 2) 기본 설정(플래그 off) — 아무것도 쓰지도 지우지도 않는다.
# ─────────────────────────────────────────────────────────────
async def test_disabled_by_default():
    assert settings.memory_forget_enabled is False


async def test_flag_off_writes_nothing():
    session = _FakeSession()
    result = await forget.apply(
        session, user_id=_UID, request=forget.ForgetRequest(scope="fact", fact_ids=(_FACT,))
    )
    assert result.status == forget.RESULT_CLASSIFIED_ONLY
    assert result.wrote_anything is False
    assert session.calls == []  # 세션을 한 번도 건드리지 않았다


async def test_flag_off_even_for_all_scope():
    session = _FakeSession()
    result = await forget.apply(
        session, user_id=_UID, request=forget.ForgetRequest(scope="all")
    )
    assert result.status == forget.RESULT_CLASSIFIED_ONLY and session.calls == []


# ─────────────────────────────────────────────────────────────
# 3) legacy 유저에게 성공을 가장하지 않는다.
# ─────────────────────────────────────────────────────────────
async def test_legacy_user_is_not_faked_success(enabled):
    session = _FakeSession(mode="legacy")
    result = await forget.apply(
        session, user_id=_UID, request=forget.ForgetRequest(scope="fact", fact_ids=(_FACT,))
    )
    assert result.status == forget.RESULT_NOT_NORMALIZED
    assert session.statements == [str(forget._LOCK_CONTEXT_SQL)]  # 잠금 읽기 외 쓰기 없음


async def test_missing_context_is_not_normalized(enabled):
    session = _FakeSession(mode=None)
    result = await forget.apply(
        session, user_id=_UID, request=forget.ForgetRequest(scope="all")
    )
    assert result.status == forget.RESULT_NOT_NORMALIZED


# ─────────────────────────────────────────────────────────────
# 4) 켰을 때 — 7단계 순서와 각 단계의 내용.
# ─────────────────────────────────────────────────────────────
async def test_seven_step_order(enabled):
    session = _FakeSession()
    result = await forget.apply(
        session, user_id=_UID, request=forget.ForgetRequest(scope="fact", fact_ids=(_FACT,))
    )
    assert result.status == forget.RESULT_APPLIED
    order = [
        session.index_of(forget._LOCK_CONTEXT_SQL),
        session.index_of(forget._INSERT_CLOSURE_SQL),
        session.index_of(forget._BUMP_SQL),
        session.index_of(forget._INSERT_MARKER_SQL),
        session.index_of(forget._FORGET_FACT_SQL),
        session.index_of(forget._INVALIDATE_INSIGHTS_SQL),
        session.index_of(forget._INVALIDATE_PROFILES_SQL),
        session.index_of(jobs._ENQUEUE_SQL),
    ]
    assert order == sorted(order)


async def test_closure_is_persisted_before_generation_bump(enabled):
    """closure가 먼저 영속돼야 retention 후 replay가 같은 구간을 되살리지 못한다."""
    session = _FakeSession()
    await forget.apply(
        session, user_id=_UID, request=forget.ForgetRequest(scope="fact", fact_ids=(_FACT,))
    )
    params = session.executed(forget._INSERT_CLOSURE_SQL)[0]
    assert (params["from_watermark"], params["through_watermark"]) == (4, 6)
    assert params["source_kind"] == memory_repo.SOURCE_KIND_CONVERSATION_TURN
    assert session.index_of(forget._INSERT_CLOSURE_SQL) < session.index_of(forget._BUMP_SQL)


async def test_predicate_scope_closes_everything_up_to_watermark(enabled):
    session = _FakeSession(watermark=12)
    await forget.apply(
        session,
        user_id=_UID,
        request=forget.ForgetRequest(scope="predicate", predicate="residence"),
    )
    params = session.executed(forget._INSERT_CLOSURE_SQL)[0]
    assert (params["from_watermark"], params["through_watermark"]) == (1, 12)
    assert not session.ran(forget._FACT_TARGETS_SQL)  # 대상 목록이 필요 없다


async def test_marker_copies_hash_and_version_verbatim(enabled):
    """hash를 다시 계산하지 않는다 — 계산식이 바뀌면 잊은 사실이 되살아난다."""
    session = _FakeSession()
    await forget.apply(
        session, user_id=_UID, request=forget.ForgetRequest(scope="fact", fact_ids=(_FACT,))
    )
    rows = session.executed(forget._INSERT_MARKER_SQL)[0]
    assert rows == [
        {
            "user_id": _UID,
            "scope": "fact",
            "fact_id": _FACT,
            "normalized_hash": "h1",
            "normalization_version": "memory-fact-v1",
            "predicate": None,
            "generation": 4,
        }
    ]


async def test_marker_generation_is_the_bumped_one(enabled):
    session = _FakeSession(generation=3)
    result = await forget.apply(
        session, user_id=_UID, request=forget.ForgetRequest(scope="all")
    )
    assert result.memory_generation == 4 and result.input_revision == 6
    assert session.executed(forget._INSERT_MARKER_SQL)[0][0]["generation"] == 4


async def test_enqueues_profile_refresh_and_vector_delete(enabled):
    session = _FakeSession()
    result = await forget.apply(
        session, user_id=_UID, request=forget.ForgetRequest(scope="fact", fact_ids=(_FACT,))
    )
    enqueued = session.executed(jobs._ENQUEUE_SQL)
    types = {p["job_type"] for p in enqueued}
    assert types == {memory_repo.JOB_PROFILE_REFRESH, forget.JOB_MEMORY_VECTOR_DELETE}
    vector = next(p for p in enqueued if p["job_type"] == forget.JOB_MEMORY_VECTOR_DELETE)
    assert vector["dedup_key"] == f"{_UID}:{result.operation_id}"
    assert vector["queue"] == jobs.QUEUE_MAINTENANCE


async def test_cascade_uses_actually_forgotten_facts(enabled):
    """insight/profile 무효화는 **실제로 닫힌** fact를 근거로 한다(요청한 id가 아니라)."""
    session = _FakeSession(forgotten=[(_FACT,), (_FACT2,)])
    result = await forget.apply(
        session, user_id=_UID, request=forget.ForgetRequest(scope="fact", fact_ids=(_FACT,))
    )
    params = session.executed(forget._INVALIDATE_INSIGHTS_SQL)[0]
    assert params["fact_ids"] == [str(_FACT), str(_FACT2)]
    assert result.invalidated_insights == (_INSIGHT,)
    assert result.invalidated_profiles == (_PROFILE,)


async def test_no_targets_writes_nothing(enabled):
    """타 유저 id·없는 id — 아무것도 쓰지 않고 성공으로도 가장하지 않는다."""
    session = _FakeSession(targets=[])
    result = await forget.apply(
        session, user_id=_UID, request=forget.ForgetRequest(scope="fact", fact_ids=(_FACT,))
    )
    assert result.status == forget.RESULT_NOTHING_MATCHED
    assert not session.ran(forget._BUMP_SQL) and not session.ran(forget._INSERT_MARKER_SQL)


async def test_unmapped_evidence_fails_closed(enabled):
    """근거의 watermark를 모르면 닫을 구간을 정할 수 없다 → 부분 삭제 금지."""
    session = _FakeSession(
        targets=[
            {
                "id": _FACT,
                "content_hash": "h1",
                "normalization_version": "memory-fact-v1",
                "status": "active",
                "from_wm": 4,
                "through_wm": 6,
                "evidence_n": 3,
                "mapped_n": 2,
            }
        ]
    )
    with pytest.raises(forget.ForgetError):
        await forget.apply(
            session, user_id=_UID, request=forget.ForgetRequest(scope="fact", fact_ids=(_FACT,))
        )
    assert not session.ran(forget._BUMP_SQL)


async def test_request_validation():
    with pytest.raises(forget.ForgetError):
        forget.ForgetRequest(scope="fact")
    with pytest.raises(forget.ForgetError):
        forget.ForgetRequest(scope="predicate")
    with pytest.raises(forget.ForgetError):
        forget.ForgetRequest(scope="everything")


# ─────────────────────────────────────────────────────────────
# 5) terminal 불가역 — 문장 구조로 못 박는다(시뮬레이터로는 못 잡는다).
# ─────────────────────────────────────────────────────────────
def test_state_changing_updates_pin_their_starting_state():
    for sql in (
        forget._FORGET_FACT_SQL,
        forget._FORGET_PREDICATE_SQL,
        forget._FORGET_ALL_FACTS_SQL,
        forget._INVALIDATE_INSIGHTS_SQL,
        forget._INVALIDATE_ALL_INSIGHTS_SQL,
    ):
        assert "status='active'" in str(sql)
    for sql in (forget._INVALIDATE_PROFILES_SQL, forget._INVALIDATE_ALL_PROFILES_SQL):
        assert "status='published'" in str(sql)


def test_no_revival_path_in_module():
    """잊은 것을 되살리는 UPDATE·marker 삭제가 모듈에 하나도 없다."""
    assert "SET status='active'" not in _SRC
    assert not re.search(r"status\s*=\s*'active'\s*,", _SRC)  # SET 절의 active 대입
    assert not re.search(r"DELETE\s+FROM\s+memory_forget_markers", _SRC, re.I)
    assert not re.search(r"DELETE\s+FROM\s+memory_source_closures", _SRC, re.I)


def test_bump_is_scoped_to_normalized_users():
    assert "memory_mode='normalized'" in str(forget._BUMP_SQL)
