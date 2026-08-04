"""저장소 불변식(W8) — cross-user 차단 / terminal 불가역 / revision 증가 조건 / turn 배정.

실 DB에 붙지 않는다. `_FakeSession`이 각 문장을 SQL 텍스트로 식별해 캔에 담긴 결과를 돌려주고,
어떤 문장이 어떤 순서로 실행됐는지를 기록한다. 시뮬레이터만 있으면 SQL을 바꿔도 통과해버리므로,
SQL 자체의 불변식(status='active' 조건·되살리는 UPDATE 부재)은 문장 구조로 따로 못 박는다.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import pytest

from app.services import jobs
from app.services import memory_candidates as mc
from app.services import memory_norm as mn
from app.services import memory_reconcile as mr
from app.services import memory_repo as repo

_UID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_MSG = 900
_REP, _ASSIST = 11, 12
_NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────
# 최소 세션 시뮬레이터
# ─────────────────────────────────────────────────────────────
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
    """repo가 실행하는 문장을 기록하고 캔 결과를 돌려준다."""

    def __init__(self, **canned) -> None:
        self.calls: list[tuple[str, object]] = []
        self.messages = canned.get(
            "messages", {_REP: ("user", "오늘 이사했어"), _ASSIST: ("moly", "어디로?")}
        )
        self.watermark = canned.get("watermark", 7)
        self.generation = canned.get("generation", 0)
        self.mode = canned.get("mode", "legacy")
        self.revision = canned.get("revision", 4)
        self.reinforce_hits = canned.get("reinforce_hits", True)
        self.supersede_hits = canned.get("supersede_hits", True)
        self.new_fact_id = uuid.uuid4()

    @property
    def statements(self) -> list[str]:
        return [c[0] for c in self.calls]

    def executed(self, sql) -> list[object]:
        return [p for s, p in self.calls if s == str(sql)]

    async def execute(self, stmt, params=None):
        s = str(stmt)
        self.calls.append((s, params))
        if s == str(repo._MESSAGES_SQL):
            ids = params["ids"]
            return _Res([{"id": i, "sender": self.messages[i][0], "content": self.messages[i][1]}
                         for i in ids if i in self.messages])
        if s == str(repo._ALLOC_SQL):
            return _Res([(self.watermark, self.generation, self.mode)])
        if s == str(repo._INSERT_FACT_SQL):
            return _Res([(self.new_fact_id,)])
        if s == str(repo._REINFORCE_SQL):
            return _Res([(params["fact_id"],)] if self.reinforce_hits else [])
        if s == str(repo._SUPERSEDE_SQL):
            return _Res([(params["fact_id"],)] if self.supersede_hits else [])
        if s == str(repo._BUMP_REVISION_SQL):
            self.revision += 1
            return _Res([(self.revision,)])
        if s in (str(repo._INSERT_TURN_SQL), str(repo._INSERT_TURN_MESSAGE_SQL),
                 str(repo._INSERT_EVIDENCE_SQL)):
            return _Res([])
        raise AssertionError(f"시뮬레이터가 모르는 문장: {s[:80]}")


def _candidate(text="부산에 산다", ids=(_REP,), predicate="residence", obj=None):
    return mc.parse_candidates(
        {
            "schema_version": mc.SCHEMA_VERSION,
            "candidates": [{
                "kind": "profile", "canonical_text": text, "importance": 0.5, "confidence": 0.8,
                "evidence_message_ids": list(ids), "predicate": predicate,
                "object_json": obj or {"city": text[:2]},
            }],
        },
        allowed_message_ids=(_REP, _ASSIST),
    )[0]


def _decision(action, *, targets=(), evidence=(_REP,), text="부산에 산다"):
    c = _candidate(text=text, ids=evidence)
    return mr.Decision(
        action=action, candidate=c, normalized=mn.normalize(c.fields()), reason="테스트",
        target_fact_ids=tuple(targets), confidence=c.confidence,
        new_evidence_message_ids=tuple(evidence),
    )


# ─────────────────────────────────────────────────────────────
# turn 배정 — watermark는 turn당 하나, 대표는 inbound user message.
# ─────────────────────────────────────────────────────────────
async def test_allocate_source_turn_links_every_message_to_one_watermark():
    s = _FakeSession()
    turn = await repo.allocate_source_turn(
        s, user_id=_UID, representative_message_id=_REP,
        message_ids=[_REP, _ASSIST], committed_at=_NOW,
    )
    assert turn.watermark == 7 and turn.message_ids == (_REP, _ASSIST)
    links = s.executed(repo._INSERT_TURN_MESSAGE_SQL)[0]
    assert [r["message_id"] for r in links] == [_REP, _ASSIST]
    assert {r["source_watermark"] for r in links} == {7}  # turn당 정확히 하나


async def test_allocate_source_turn_includes_representative_even_if_omitted():
    s = _FakeSession()
    turn = await repo.allocate_source_turn(
        s, user_id=_UID, representative_message_id=_REP, message_ids=[_ASSIST], committed_at=_NOW,
    )
    assert _REP in turn.message_ids


async def test_greeting_only_turn_is_not_a_source():
    """대표가 inbound user message가 아니면 추출 소스를 만들지 않는다(선발화만 있는 turn 등)."""
    s = _FakeSession(messages={_ASSIST: ("moly", "자니?")})
    with pytest.raises(repo.NoInboundUserMessageError):
        await repo.allocate_source_turn(
            s, user_id=_UID, representative_message_id=_ASSIST,
            message_ids=[_ASSIST], committed_at=_NOW,
        )
    assert str(repo._ALLOC_SQL) not in s.statements  # watermark를 소모하지도 않는다
    assert str(repo._INSERT_TURN_SQL) not in s.statements


async def test_allocate_rejects_message_of_another_user():
    s = _FakeSession()  # _OTHER_MSG는 이 유저 조회 결과에 없다 = 남의 메시지
    with pytest.raises(repo.CrossUserEvidenceError):
        await repo.allocate_source_turn(
            s, user_id=_UID, representative_message_id=_REP,
            message_ids=[_REP, _OTHER_MSG], committed_at=_NOW,
        )
    assert str(repo._INSERT_TURN_SQL) not in s.statements


# ─────────────────────────────────────────────────────────────
# cross-user evidence — FK가 못 잡는 자리라 코드가 유일한 방어선이다.
# ─────────────────────────────────────────────────────────────
async def test_add_evidence_rejects_cross_user_message_and_writes_nothing():
    s = _FakeSession()
    with pytest.raises(repo.CrossUserEvidenceError):
        await repo.add_evidence(
            s, user_id=_UID, fact_id=uuid.uuid4(), message_ids=[_OTHER_MSG], observed_at=_NOW,
        )
    assert str(repo._INSERT_EVIDENCE_SQL) not in s.statements


async def test_add_evidence_verifies_ownership_before_inserting():
    s = _FakeSession()
    await repo.add_evidence(
        s, user_id=_UID, fact_id=uuid.uuid4(), message_ids=[_REP], observed_at=_NOW,
    )
    # 소유권 조회가 evidence insert보다 **먼저** 실행된다(같은 트랜잭션 안).
    assert s.statements.index(str(repo._MESSAGES_SQL)) < s.statements.index(
        str(repo._INSERT_EVIDENCE_SQL)
    )
    rows = s.executed(repo._INSERT_EVIDENCE_SQL)[0]
    assert rows[0]["source_type"] == repo.SOURCE_KIND_CONVERSATION_TURN
    assert rows[0]["source_excerpt_hash"] == hashlib.sha256(
        "오늘 이사했어".encode()
    ).hexdigest()


async def test_apply_decisions_rejects_cross_user_evidence_up_front():
    from dataclasses import replace

    s = _FakeSession()
    # 후보 스키마를 통과한 뒤에도(=payload 안 id였어도) 저장소가 소유권을 다시 본다.
    d = replace(_decision(mc.ACTION_ADD), new_evidence_message_ids=(_OTHER_MSG,))
    with pytest.raises(repo.CrossUserEvidenceError):
        await repo.apply_decisions(s, user_id=_UID, decisions=[d], observed_at=_NOW)
    assert str(repo._INSERT_FACT_SQL) not in s.statements  # fact도 만들지 않는다


async def test_payload_message_ids_must_match_the_range_exactly(monkeypatch):
    class _S(_FakeSession):
        async def execute(self, stmt, params=None):
            if str(stmt) == str(repo._RANGE_MESSAGES_SQL):
                return _Res([(_REP,), (_ASSIST,)])
            return await super().execute(stmt, params)

    s = _S()
    await repo.assert_payload_message_ids(
        s, _UID, from_watermark=1, through_watermark=2, message_ids=[_ASSIST, _REP]
    )
    with pytest.raises(repo.CrossUserEvidenceError):  # 누락
        await repo.assert_payload_message_ids(
            s, _UID, from_watermark=1, through_watermark=2, message_ids=[_REP]
        )
    with pytest.raises(repo.CrossUserEvidenceError):  # 초과(타 구간·타 유저 id)
        await repo.assert_payload_message_ids(
            s, _UID, from_watermark=1, through_watermark=2,
            message_ids=[_REP, _ASSIST, _OTHER_MSG],
        )


# ─────────────────────────────────────────────────────────────
# terminal 불가역
# ─────────────────────────────────────────────────────────────
async def test_reinforce_on_non_active_fact_fails_instead_of_reviving():
    s = _FakeSession(reinforce_hits=False)  # UPDATE 0행 = 그 사이 forgotten/superseded
    d = _decision(mc.ACTION_REINFORCE, targets=(uuid.uuid4(),))
    with pytest.raises(repo.TerminalFactError):
        await repo.apply_decisions(s, user_id=_UID, decisions=[d], observed_at=_NOW)
    assert str(repo._BUMP_REVISION_SQL) not in s.statements


async def test_supersede_on_non_active_fact_fails_instead_of_reviving():
    s = _FakeSession(supersede_hits=False)
    d = _decision(mc.ACTION_SUPERSEDE, targets=(uuid.uuid4(),))
    with pytest.raises(repo.TerminalFactError):
        await repo.apply_decisions(s, user_id=_UID, decisions=[d], observed_at=_NOW)


def _sql(stmt) -> str:
    """공백을 지운 SQL — 서식 변경에 흔들리지 않게(test_jobs와 같은 방식)."""
    return "".join(str(stmt).split())


def test_every_state_changing_update_requires_active():
    for stmt in (repo._REINFORCE_SQL, repo._SUPERSEDE_SQL):
        assert "status='active'" in _sql(stmt).split("WHERE")[1]
        assert "user_id=:user_id" in _sql(stmt)  # 항상 유저 스코프


def _update_statements() -> list[str]:
    """모듈의 `*_SQL` 상수 중 UPDATE 문 전부(새 문장이 추가돼도 자동으로 검사 대상이 된다)."""
    out = []
    for name in dir(repo):
        if not name.endswith("_SQL"):
            continue
        s = _sql(getattr(repo, name))
        if s.upper().startswith("UPDATE") or "DOUPDATESET" in s.upper():
            out.append(s)
    return out


def test_no_statement_ever_sets_status_back_to_active():
    """forgotten/superseded를 되살리는 UPDATE 경로가 하나도 없어야 한다."""
    stmts = _update_statements()
    assert len(stmts) >= 4  # reinforce/supersede + upsert 2종
    for s in stmts:
        set_clause = s.split("WHERE")[0]
        assert "status='active'" not in set_clause
        assert "status='" not in set_clause or "status='superseded'" in set_clause
    # active를 만드는 것은 INSERT 하나뿐이다.
    assert "'active'" in _sql(repo._INSERT_FACT_SQL)


def test_repo_never_deletes_memory_rows():
    import inspect

    src = inspect.getsource(repo).upper()
    assert "DELETE FROM" not in src and "TRUNCATE" not in src


# ─────────────────────────────────────────────────────────────
# revision — 실제 변경 때만 정확히 +1.
# ─────────────────────────────────────────────────────────────
async def test_ignore_only_batch_changes_nothing_and_does_not_bump_revision():
    s = _FakeSession()
    d = mr.Decision(
        action=mc.ACTION_IGNORE, candidate=_candidate(), normalized=mn.normalize(_candidate().fields()),
        reason="no_new_evidence",
    )
    res = await repo.apply_decisions(s, user_id=_UID, decisions=[d, d], observed_at=_NOW)
    assert res.changed is False and res.revision is None and res.ignored == 2
    assert s.calls == []  # 조회조차 하지 않는다(근거로 쓸 메시지가 없다)


async def test_multiple_changes_bump_revision_exactly_once():
    s = _FakeSession()
    decisions = [
        _decision(mc.ACTION_ADD, text="부산에 산다"),
        _decision(mc.ACTION_KEEP_BOTH, text="대구에 산다"),
        _decision(mc.ACTION_REINFORCE, targets=(uuid.uuid4(),)),
    ]
    res = await repo.apply_decisions(s, user_id=_UID, decisions=decisions, observed_at=_NOW)
    assert res.changed is True and res.added == 2 and res.reinforced == 1
    assert s.statements.count(str(repo._BUMP_REVISION_SQL)) == 1
    assert res.revision == 5


async def test_supersede_inserts_new_fact_before_closing_the_old_one():
    s = _FakeSession()
    old = uuid.uuid4()
    res = await repo.apply_decisions(
        s, user_id=_UID, decisions=[_decision(mc.ACTION_SUPERSEDE, targets=(old,))],
        observed_at=_NOW,
    )
    assert res.superseded == 1
    assert s.statements.index(str(repo._INSERT_FACT_SQL)) < s.statements.index(
        str(repo._SUPERSEDE_SQL)
    )
    assert s.executed(repo._SUPERSEDE_SQL)[0]["superseded_by"] == s.new_fact_id


async def test_stored_natural_language_is_masked_at_write_time():
    s = _FakeSession()
    d = _decision(mc.ACTION_ADD, text="승민은 부산에 산다")
    await repo.apply_decisions(
        s, user_id=_UID, decisions=[d], observed_at=_NOW, nickname="승민"
    )
    stored = s.executed(repo._INSERT_FACT_SQL)[0]["canonical_text"]
    assert "승민" not in stored and "{유저이름}" in stored


def test_vector_search_is_user_scoped_and_hard_filters_forgotten_sources():
    sql = _sql(repo._SEARCH_MEMORY_SQL)
    assert "f.user_id=:user_id" in sql
    assert "i.user_id=:user_id" in sql
    assert "memory_forget_markers" in sql
    assert "f.status='active'" in sql and "i.status='active'" in sql


def test_embedding_write_cannot_restore_forgotten_or_marked_fact():
    sql = _sql(repo._WRITE_EMBEDDING_SQL)
    assert "f.user_id=:user_id" in sql and "f.status='active'" in sql
    assert "NOTEXISTS" in sql and "memory_forget_markers" in sql


# ─────────────────────────────────────────────────────────────
# 잡 연동 — W7 enqueue API만 쓴다.
# ─────────────────────────────────────────────────────────────
async def test_extraction_job_goes_to_content_queue_with_generation_scoped_dedup(monkeypatch):
    seen = {}

    async def _fake_enqueue(session, **kw):
        seen.update(kw)
        return uuid.uuid4()

    monkeypatch.setattr(jobs, "enqueue", _fake_enqueue)
    await repo.enqueue_extraction(
        _FakeSession(), user_id=_UID, memory_generation=3,
        from_watermark=7, through_watermark=9, message_ids=[_REP, _ASSIST],
    )
    assert seen["queue"] == jobs.QUEUE_CONTENT and seen["job_type"] == repo.JOB_MEMORY_EXTRACT
    assert seen["dedup_key"] == f"{_UID}:3:7:9"  # generation이 들어가야 세대 간 잡이 안 합쳐진다
    p = seen["payload"]
    assert p["schema_version"] == repo.SOURCE_PAYLOAD_SCHEMA_VERSION
    assert p["source_kind"] == repo.SOURCE_KIND_CONVERSATION_TURN
    assert (p["source_from_watermark"], p["source_through_watermark"]) == (7, 9)
    assert p["message_ids"] == [_REP, _ASSIST] and p["memory_generation"] == 3
