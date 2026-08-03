"""판정 규칙(W8) — 코드가 정하고 LLM은 제안만 한다.

DB에 붙지 않는다. 판정은 순수 함수라 상태를 그대로 넣고 결과만 본다.
"""
from __future__ import annotations

import uuid

import pytest

from app.services import memory_candidates as mc
from app.services import memory_norm as mn
from app.services import memory_reconcile as mr

_M1, _M2, _M3 = 101, 102, 103
_WM = {_M1: 5, _M2: 5, _M3: 9}  # message_id → source_watermark


def _cands(*, text="서울에 산다", predicate="residence", obj=None, ids=(_M1,), conf=0.8,
           kind="profile", proposed=None):
    c = {
        "kind": kind, "canonical_text": text, "importance": 0.5, "confidence": conf,
        "evidence_message_ids": list(ids),
    }
    if predicate is not None:
        c["predicate"] = predicate
        c["object_json"] = obj if obj is not None else {"city": text[:2]}
    if proposed is not None:
        c["proposed_action"] = proposed
    return mc.parse_candidates(
        {"schema_version": mc.SCHEMA_VERSION, "candidates": [c]},
        allowed_message_ids=(_M1, _M2, _M3),
    )


def _existing(candidate, *, wm=5, evidence=(_M1,), conf=0.5, fact_id=None):
    """후보와 **같은 내용**의 기존 active fact(같은 hash)."""
    n = mn.normalize(candidate.fields())
    return mr.ExistingFact(
        id=fact_id or uuid.uuid4(), kind=n.kind, subject=n.subject, predicate=n.predicate,
        content_hash=n.content_hash, normalization_version=n.version, confidence=conf,
        evidence_message_ids=frozenset(evidence), max_source_watermark=wm,
    )


def _decide(cands, existing=(), markers=(), watermarks=None):
    return mr.decide(cands, existing=list(existing), markers=list(markers),
                     watermarks=_WM if watermarks is None else watermarks)


# ─────────────────────────────────────────────────────────────
# 1. hard filter가 가장 먼저 — LLM 제안을 덮어쓴다.
# ─────────────────────────────────────────────────────────────
def test_forget_all_marker_ignores_everything():
    d = _decide(_cands(proposed=mc.ACTION_ADD), markers=[mr.ForgetMarker(scope=mr.SCOPE_ALL)])
    assert d[0].action == mc.ACTION_IGNORE and d[0].reason == "forget_all"


def test_predicate_marker_ignores_that_predicate_only():
    m = [mr.ForgetMarker(scope=mr.SCOPE_PREDICATE, predicate="residence")]
    assert _decide(_cands(predicate="residence"), markers=m)[0].action == mc.ACTION_IGNORE
    assert _decide(_cands(predicate="likes", text="커피"), markers=m)[0].action == mc.ACTION_ADD


def test_fact_hash_marker_ignores_matching_candidate():
    c = _cands()[0]
    n = mn.normalize(c.fields())
    m = [mr.ForgetMarker(scope=mr.SCOPE_FACT, normalized_hash=n.content_hash,
                         normalization_version=n.version)]
    d = _decide([c], markers=m)
    assert d[0].action == mc.ACTION_IGNORE and d[0].reason == "forget_fact"


def test_hard_filter_beats_llm_suggestion_and_existing_match():
    """marker에 걸린 후보는 REINFORCE 대상이 있어도 IGNORE다(hard filter가 먼저)."""
    c = _cands(ids=(_M1, _M2), proposed=mc.ACTION_REINFORCE)[0]
    n = mn.normalize(c.fields())
    d = _decide(
        [c],
        existing=[_existing(c, evidence=(_M1,))],
        markers=[mr.ForgetMarker(scope=mr.SCOPE_FACT, normalized_hash=n.content_hash,
                                 normalization_version=n.version)],
    )
    assert d[0].action == mc.ACTION_IGNORE


class _FakeV2:
    """산출물이 다른 가상의 후속 version(구 version marker 재계산 검증용)."""

    version = "memory-fact-v2-test"

    def normalize(self, fields):
        n = mn.get_normalizer(mn.NORMALIZATION_VERSION_V1).normalize(fields)
        return mn.NormalizedFact(
            version=self.version, kind=n.kind, canonical_text=n.canonical_text,
            subject=n.subject, predicate=n.predicate, object_json=n.object_json,
            event_time=n.event_time, content_hash="v2:" + n.content_hash,
        )


@pytest.fixture
def fake_v2():
    normalizer = _FakeV2()
    mn.register(normalizer)
    try:
        yield normalizer
    finally:
        mn._NORMALIZERS.pop(normalizer.version, None)


def test_hard_filter_recomputes_hash_for_every_marker_version(fake_v2):
    """현재 version으로만 계산하면 다른 version의 marker를 그냥 지나친다 = 잊은 사실 부활."""
    c = _cands()[0]
    v2_hash = mn.content_hash(c.fields(), version=fake_v2.version)
    assert v2_hash != mn.content_hash(c.fields())  # 두 version의 산출물이 실제로 다르다
    d = _decide([c], markers=[mr.ForgetMarker(scope=mr.SCOPE_FACT, normalized_hash=v2_hash,
                                              normalization_version=fake_v2.version)])
    assert d[0].action == mc.ACTION_IGNORE and d[0].reason == "forget_fact"


def test_unsupported_marker_version_refuses_to_publish():
    """지원 못 하는 marker version이 있으면 조용히 넘어가지 않고 job을 실패시킨다."""
    with pytest.raises(mn.UnsupportedNormalizationVersionError):
        _decide(_cands(), markers=[mr.ForgetMarker(
            scope=mr.SCOPE_FACT, normalized_hash="x", normalization_version="memory-fact-v99")])


def test_unsupported_existing_fact_version_also_refuses():
    c = _cands()[0]
    stale = _existing(c)
    stale = mr.ExistingFact(
        id=stale.id, kind=stale.kind, subject=stale.subject, predicate=stale.predicate,
        content_hash=stale.content_hash, normalization_version="memory-fact-v99",
        confidence=stale.confidence, evidence_message_ids=stale.evidence_message_ids,
        max_source_watermark=stale.max_source_watermark,
    )
    with pytest.raises(mn.UnsupportedNormalizationVersionError):
        _decide([c], existing=[stale])


# ─────────────────────────────────────────────────────────────
# 2~3. REINFORCE / ADD
# ─────────────────────────────────────────────────────────────
def test_same_hash_with_new_evidence_reinforces_and_takes_max_confidence():
    c = _cands(ids=(_M1, _M2), conf=0.9)[0]
    ex = _existing(c, evidence=(_M1,), conf=0.4)
    d = _decide([c], existing=[ex])[0]
    assert d.action == mc.ACTION_REINFORCE and d.target_fact_ids == (ex.id,)
    assert d.new_evidence_message_ids == (_M2,)  # 이미 있는 근거는 다시 달지 않는다
    assert d.confidence == 0.9


def test_same_hash_without_new_evidence_is_noop_ignore():
    """retry/재실행 — 상태를 건드리면 revision이 헛돌아 프로필이 계속 재생성된다."""
    c = _cands(ids=(_M1,), conf=0.1)[0]
    d = _decide([c], existing=[_existing(c, evidence=(_M1,), conf=0.9)])[0]
    assert d.action == mc.ACTION_IGNORE and d.reason == "no_new_evidence"
    assert d.changes_state is False


def test_new_identity_adds():
    d = _decide(_cands())[0]
    assert d.action == mc.ACTION_ADD and d.changes_state is True


def test_predicateless_candidate_adds_when_hash_differs():
    other = _cands(text="요즘 지쳤다", predicate=None, kind="emotion")[0]
    existing = _existing(_cands(text="요즘 신났다", predicate=None, kind="emotion")[0])
    assert _decide([other], existing=[existing])[0].action == mc.ACTION_ADD


# ─────────────────────────────────────────────────────────────
# 4. cardinality + watermark 최신성
# ─────────────────────────────────────────────────────────────
def test_multi_predicate_keeps_both():
    ex = _existing(_cands(predicate="likes", text="커피", ids=(_M1,))[0], wm=5)
    d = _decide(_cands(predicate="likes", text="녹차", ids=(_M3,)), existing=[ex])[0]
    assert d.action == mc.ACTION_KEEP_BOTH


def test_single_predicate_supersedes_only_when_watermark_is_newer():
    ex = _existing(_cands(text="서울에 산다", ids=(_M1,))[0], wm=5)
    newer = _decide(_cands(text="부산에 산다", ids=(_M3,)), existing=[ex])[0]  # wm 9 > 5
    assert newer.action == mc.ACTION_SUPERSEDE and newer.target_fact_ids == (ex.id,)


def test_older_observation_is_ignored_not_superseded():
    ex = _existing(_cands(text="서울에 산다", ids=(_M3,))[0], wm=9)
    older = _decide(_cands(text="부산에 산다", ids=(_M1,)), existing=[ex])[0]  # wm 5 < 9
    assert older.action == mc.ACTION_IGNORE and older.reason == "older_observation"


def test_watermark_tie_conflict_publishes_nothing():
    """동률 + 내용 충돌 = 어느 쪽이 최신인지 알 수 없다 → LLM 제안으로 깨지 않고 실패시킨다."""
    ex = _existing(_cands(text="서울에 산다", ids=(_M1,))[0], wm=5)
    with pytest.raises(mr.ReconcileConflictError):
        _decide(_cands(text="부산에 산다", ids=(_M2,), proposed=mc.ACTION_SUPERSEDE), existing=[ex])


def test_existing_fact_without_known_watermark_is_a_conflict_not_a_supersede():
    ex = _existing(_cands(text="서울에 산다")[0], wm=None)
    with pytest.raises(mr.ReconcileConflictError):
        _decide(_cands(text="부산에 산다", ids=(_M3,)), existing=[ex])


def test_evidence_without_watermark_mapping_fails():
    """최신성 비교가 필요한데 근거의 watermark를 모르면 추측하지 않고 실패시킨다."""
    ex = _existing(_cands(text="서울에 산다", ids=(_M1,))[0], wm=5)
    with pytest.raises(mr.ReconcileError):
        _decide(_cands(text="부산에 산다", ids=(_M3,)), existing=[ex], watermarks={})


# ─────────────────────────────────────────────────────────────
# 부분 publish 금지 + 배치 내 중복 제거
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "closure",
    [
        mr.Closure(from_watermark=5, through_watermark=5),    # 시작과 겹침
        mr.Closure(from_watermark=7, through_watermark=7),    # **중간**만 겹침
        mr.Closure(from_watermark=9, through_watermark=20),   # 끝과 겹침
        mr.Closure(from_watermark=1, through_watermark=100),  # 전체 포함
    ],
)
def test_any_overlap_closes_the_whole_range(closure):
    with pytest.raises(mr.SourceRangeClosedError):
        mr.assert_source_range_open([closure], 5, 9)


def test_disjoint_closure_does_not_block():
    mr.assert_source_range_open([mr.Closure(from_watermark=1, through_watermark=4)], 5, 9)
    mr.assert_source_range_open([mr.Closure(from_watermark=10, through_watermark=12)], 5, 9)


def test_duplicate_candidates_in_one_batch_merge_evidence():
    c1 = _cands(ids=(_M1,), conf=0.4)[0]
    c2 = _cands(ids=(_M2,), conf=0.9)[0]  # 같은 내용, 다른 근거
    d = _decide([c1, c2])
    assert len(d) == 1 and d[0].action == mc.ACTION_ADD
    assert set(d[0].new_evidence_message_ids) == {_M1, _M2}
    assert d[0].confidence == 0.9


def test_decisions_never_revive_a_terminal_fact():
    """terminal fact는 비교 대상(existing=active)에 아예 들어오지 않으므로 되살릴 판정이 없다."""
    c = _cands()[0]
    d = _decide([c], existing=[])[0]  # forgotten이라 active 목록에 없는 상황
    assert d.action == mc.ACTION_ADD  # 기존 행 수정이 아니라 새 행
    assert d.target_fact_ids == ()
