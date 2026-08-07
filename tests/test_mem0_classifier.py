"""classifier 파서 — 자유문 판정과 없는 id를 거부한다.

받아주면 validator가 검사할 대상 자체가 오염된다.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.services import mem0_classifier as mc
from app.services.mem0_consolidation import Verdict

A, B = uuid.UUID(int=1), uuid.UUID(int=2)
KNOWN = {A, B}


def _out(**kw):
    edge = {"subject": str(A), "target": str(B), "verdict": "supersedes"}
    edge.update(kw)
    return json.dumps({"edges": [edge]})


def test_valid_edge_is_parsed():
    edges = mc.parse(_out(), known_ids=KNOWN)
    assert edges == [mc.Edge(A, B, Verdict.SUPERSEDES)]


def test_empty_edges_is_valid():
    """관계가 없으면 edge를 만들지 않는 게 정상이다."""
    assert mc.parse('{"edges":[]}', known_ids=KNOWN) == []


def test_free_form_verdict_is_rejected():
    with pytest.raises(mc.ClassifierSchemaError):
        mc.parse(_out(verdict="아마 같은 얘기인 듯"), known_ids=KNOWN)


def test_unknown_id_is_rejected():
    """모델이 만들어낸 id — 그래프 전체를 쓰지 않는다."""
    with pytest.raises(mc.ClassifierSchemaError):
        mc.parse(_out(target=str(uuid.uuid4())), known_ids=KNOWN)


def test_non_uuid_id_is_rejected():
    with pytest.raises(mc.ClassifierSchemaError):
        mc.parse(_out(subject="첫번째"), known_ids=KNOWN)


@pytest.mark.parametrize("payload", ["nope", '{"edges": {}}', '[{"subject":"x"}]'])
def test_broken_shape_is_rejected(payload):
    with pytest.raises(mc.ClassifierSchemaError):
        mc.parse(payload, known_ids=KNOWN)


def test_all_four_verdicts_accepted():
    for v in ("independent", "duplicate", "supersedes", "ambiguous"):
        edges = mc.parse(_out(verdict=v), known_ids=KNOWN)
        assert edges[0].verdict.value == v


def test_render_separates_new_and_existing():
    text = mc.render_pairs([(A, "커피 좋아함")], [(B, "차 좋아함")])
    assert "[신규]" in text and "[기존]" in text
    assert str(A) in text and str(B) in text


def test_render_omits_existing_section_when_none():
    text = mc.render_pairs([(A, "커피")], [])
    assert "[기존]" not in text


def test_batch_limit_is_documented():
    """memory마다 top-k와 classifier를 반복하면 호출 수가 후보 수만큼 늘어난다."""
    assert mc.MAX_EXISTING_CANDIDATES == 12


def test_prompt_prefers_ambiguous_over_guessing():
    assert "확신이 없으면 ambiguous" in mc.build_system()
