"""classifier 파서 — 자유문 판정과 없는 id를 거부한다.

받아주면 validator가 검사할 대상 자체가 오염된다. 다만 **거부 범위가 좁아졌다**:
edge 하나의 문제는 그 edge만 버린다. 예전에는 전량 거부라 8번 재시도 뒤 잡이 죽었고,
그 턴의 기억은 영원히 미판정으로 남아 회상에서 아예 안 보였다.
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


# ── 출력을 통째로 못 믿을 때만 전량 거부 ─────────────────────
@pytest.mark.parametrize("payload", ["nope", '{"edges": {}}', '[{"subject":"x"}]'])
def test_broken_shape_is_rejected(payload):
    with pytest.raises(mc.ClassifierSchemaError):
        mc.parse(payload, known_ids=KNOWN)


# ── edge 하나의 문제는 그 edge만 버린다 ──────────────────────
@pytest.mark.parametrize("bad", [
    {"verdict": "아마 같은 얘기인 듯"},   # 자유문 판정
    {"target": str(uuid.uuid4())},        # 입력에 없는 id
    {"subject": "첫번째"},                 # UUID가 아님
])
def test_bad_edge_is_skipped_not_fatal(bad):
    assert mc.parse(_out(**bad), known_ids=KNOWN) == []


def test_one_bad_edge_does_not_kill_the_good_ones():
    payload = json.dumps({"edges": [
        {"subject": str(A), "target": str(B), "verdict": "duplicate"},
        {"subject": str(A), "target": str(uuid.uuid4()), "verdict": "supersedes"},
    ]})
    edges = mc.parse(payload, known_ids=KNOWN)
    assert len(edges) == 1 and edges[0].verdict is Verdict.DUPLICATE


def test_all_four_verdicts_accepted():
    for v in ("independent", "duplicate", "supersedes", "ambiguous"):
        edges = mc.parse(_out(verdict=v), known_ids=KNOWN)
        assert edges[0].verdict.value == v


# ── 언어별 프롬프트 ──────────────────────────────────────────
#
# 기억 본문은 유저 언어로 저장되는데 비교 지시만 한국어면, 한국어 어미 예시가
# 일본어·영어 기억에는 아무 도움이 안 된다. ja·en 유저가 159명이다.
def test_render_separates_new_and_existing():
    text = mc.render_pairs([(A, "커피 좋아함")], [(B, "차 좋아함")], language="ko")
    assert "[신규]" in text and "[기존]" in text
    assert str(A) in text and str(B) in text


def test_render_omits_existing_section_when_none():
    assert "[기존]" not in mc.render_pairs([(A, "커피")], [], language="ko")


@pytest.mark.parametrize("lang,new_label", [("ko", "[신규]"), ("en", "[new]"), ("ja", "[新規]")])
def test_labels_follow_the_language(lang, new_label):
    """한국어 라벨을 비한국어 프롬프트에 섞으면 모델이 번역하려 든다."""
    assert new_label in mc.render_pairs([(A, "x")], [], language=lang)


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_non_korean_prompt_has_no_korean(lang):
    sys = mc.build_system(lang)
    assert not [c for c in sys if "가" <= c <= "힣"], f"{lang} 판정 프롬프트에 한국어가 남았다"


@pytest.mark.parametrize("lang", ["ko", "ja", "en"])
def test_every_language_prefers_ambiguous_over_guessing(lang):
    sys = mc.build_system(lang)
    assert "ambiguous" in sys
    # 대체 방향을 못 박아야 옛 기억이 새 기억을 덮지 않는다.
    assert "supersede" in sys.lower()


# ── 상한 ─────────────────────────────────────────────────────
def test_comparison_pool_matches_the_chunk_size():
    """덩어리 하나가 후보를 24건까지 만든다 — 12면 직전 덩어리의 절반만 비교한다."""
    from app.services import mem0_ingest as mi
    assert mc.MAX_EXISTING_CANDIDATES == mi.MAX_CANDIDATES_PER_CHUNK


def test_output_cap_fits_the_worst_case_item_count():
    """신규 24 + 기존 24를 다루는데 상한이 작으면 JSON이 안 닫혀 판정이 통째로 실패한다.

    운영에서 상한 900으로 34번 잘렸다. edge 하나가 UUID 두 개 + verdict라 약 50토큰이다.
    """
    from app.services import mem0_ingest as mi
    worst_items = mi.MAX_CANDIDATES_PER_CHUNK + mc.MAX_EXISTING_CANDIDATES
    assert mc.MAX_OUTPUT_TOKENS >= worst_items * 50
