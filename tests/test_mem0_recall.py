"""v2 회상 — 폐기된 기억이 프롬프트로 돌아오지 않는다(9.4절).

mem0는 candidate-add-only라 과거와 현재의 상반된 기억이 벡터 저장소에 함께 남는다.
벡터 검색만 믿으면 사용자가 "이제 회사 안 다녀"라고 말한 뒤에도 캐피가 예전 직장을 꺼낸다.
그래서 여기 테스트의 핵심은 **무엇이 안 나오는가**다.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app.services import mem0_recall as mr

UID = uuid.uuid4()


@dataclass
class _Hit:
    id: str
    distance: float
    payload: dict


class _Adapter:
    def __init__(self, hits, boom=False):
        self._hits, self._boom = hits, boom
        self.calls = 0

    async def search(self, vector, *, user_id, limit, timeout):
        self.calls += 1
        if self._boom:
            raise RuntimeError("provider down")
        return self._hits


class _Session:
    """registry 조회를 흉내낸다. `rows`는 (pid, status, occurred_at, group) 튜플."""

    def __init__(self, rows, boom=False):
        self._rows, self._boom = rows, boom
        self.params = None

    async def execute(self, stmt, params=None):
        if self._boom:
            raise RuntimeError("db down")
        self.params = params
        return _Result(self._rows)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


async def _embed(q):
    return [0.1] * 8


def _hit(pid, distance=0.5, text="회사에 다닌다"):
    return _Hit(id=pid, distance=distance, payload={"text": text, "user_id": str(UID)})


async def test_active_memory_is_returned():
    a = _Adapter([_hit("p1")])
    s = _Session([("p1", "active", None, None)])
    got = await mr.recall(s, UID, query="회사", adapter=a, embed_query=_embed)
    assert [g.text for g in got] == ["회사에 다닌다"]


@pytest.mark.parametrize("status", ["superseded", "duplicate", "pending", "excluded",
                                    "rejected_policy"])
async def test_non_visible_statuses_never_surface(status):
    """registry가 걸러야 할 상태가 하나라도 새면 옛 사실이 되살아난다.

    실제 필터는 SQL의 `semantic_status = ANY(:statuses)`가 한다. 여기서는 그 목록에
    이 상태들이 들어 있지 않다는 계약을 고정한다.
    """
    assert status not in mr.VISIBLE_STATUSES


def test_visible_statuses_are_exactly_active_and_ambiguous():
    """목록이 넓어지면 폐기된 기억이 조용히 돌아온다."""
    assert set(mr.VISIBLE_STATUSES) == {"active", "ambiguous"}


async def test_query_filter_asks_only_for_visible_statuses():
    """SQL에 넘기는 상태 목록이 VISIBLE_STATUSES와 같아야 한다."""
    a = _Adapter([_hit("p1")])
    s = _Session([("p1", "active", None, None)])
    await mr.recall(s, UID, query="회사", adapter=a, embed_query=_embed)
    assert s.params["statuses"] == list(mr.VISIBLE_STATUSES)
    assert s.params["user_id"] == UID


async def test_hit_without_registry_row_is_dropped():
    """registry에 없는 벡터는 판정되지 않은 것이다 — 보여주면 안 된다."""
    a = _Adapter([_hit("orphan")])
    s = _Session([])          # registry에 아무것도 없다
    assert await mr.recall(s, UID, query="회사", adapter=a, embed_query=_embed) == []


async def test_registry_row_without_a_hit_is_ignored():
    """검색에 안 걸린 기억을 registry만 보고 끌어오지 않는다."""
    a = _Adapter([_hit("p1")])
    s = _Session([("p1", "active", None, None), ("p2", "active", None, None)])
    got = await mr.recall(s, UID, query="회사", adapter=a, embed_query=_embed)
    assert len(got) == 1


async def test_hit_without_text_payload_is_dropped():
    a = _Adapter([_Hit(id="p1", distance=0.5, payload={"user_id": str(UID)})])
    s = _Session([("p1", "active", None, None)])
    assert await mr.recall(s, UID, query="회사", adapter=a, embed_query=_embed) == []


# ── 실패해도 대화는 살아야 한다 ─────────────────────────────

async def test_provider_failure_returns_empty_not_raise():
    """회상은 대화의 보조지 전제가 아니다. provider가 죽었다고 대화가 죽으면 안 된다."""
    a = _Adapter([], boom=True)
    s = _Session([])
    assert await mr.recall(s, UID, query="회사", adapter=a, embed_query=_embed) == []


async def test_registry_failure_returns_empty_not_raise():
    a = _Adapter([_hit("p1")])
    s = _Session([], boom=True)
    assert await mr.recall(s, UID, query="회사", adapter=a, embed_query=_embed) == []


async def test_embedding_failure_returns_empty_not_raise():
    async def _boom(q):
        raise RuntimeError("embed down")

    a = _Adapter([_hit("p1")])
    assert await mr.recall(_Session([]), UID, query="회사", adapter=a, embed_query=_boom) == []


async def test_empty_query_skips_the_provider_entirely():
    """빈 질의로 검색을 부르면 돈만 쓰고 결과는 무의미하다."""
    a = _Adapter([_hit("p1")])
    assert await mr.recall(_Session([]), UID, query="  ", adapter=a, embed_query=_embed) == []
    assert a.calls == 0


# ── ambiguous 처리 ────────────────────────────────────────

async def test_ambiguous_is_kept_not_discarded():
    """판정 못 한 건 '모른다'이지 '없다'가 아니다."""
    a = _Adapter([_hit("p1", text="회사에 다닌다"), _hit("p2", text="회사를 그만뒀다")])
    g = uuid.uuid4()
    s = _Session([("p1", "ambiguous", None, g), ("p2", "ambiguous", None, g)])
    got = await mr.recall(s, UID, query="회사", adapter=a, embed_query=_embed)
    assert len(got) == 2 and all(x.uncertain for x in got)


def test_render_tells_capi_not_to_assert_when_uncertain():
    """캐피가 단정해버리면 틀렸을 때 사용자가 정정해야 한다 — 그건 기억이 아니라 사고다."""
    items = [
        mr.Recalled("회사를 그만뒀다", "ambiguous", 0.5,
                    datetime(2026, 8, 1, tzinfo=timezone.utc), uuid.uuid4()),
    ]
    out = mr.render_block(items)
    assert "단정하지" in out
    assert "2026-08-01" in out


def test_render_is_empty_when_nothing_recalled():
    """빈 블록을 넣으면 프리픽스만 늘고 의미가 없다."""
    assert mr.render_block([]) == ""


def test_render_separates_sure_from_unsure():
    items = [
        mr.Recalled("고양이를 키운다", "active", 0.3, None, None),
        mr.Recalled("회사를 그만뒀다", "ambiguous", 0.4, None, uuid.uuid4()),
    ]
    out = mr.render_block(items)
    assert out.index("고양이를 키운다") < out.index("단정하지")


async def test_results_are_capped_by_limit():
    hits = [_hit(f"p{i}", distance=0.1 + i / 100) for i in range(20)]
    rows = [(f"p{i}", "active", None, None) for i in range(20)]
    got = await mr.recall(_Session(rows), UID, query="q",
                          adapter=_Adapter(hits), embed_query=_embed, limit=5)
    assert len(got) == 5


async def test_closer_distance_comes_first():
    """vecs는 **거리**를 준다 — 낮을수록 관련 있다. 내림차순으로 정렬하면 가장 관련 없는
    기억이 프롬프트에 실린다(실측 사고: '여자친구' 질의에 '물 마시기 루틴'이 1등이었다).
    """
    a = _Adapter([_hit("p1", distance=0.80, text="덜 관련"),
                  _hit("p2", distance=0.20, text="더 관련")])
    s = _Session([("p1", "active", None, None), ("p2", "active", None, None)])
    got = await mr.recall(s, UID, query="q", adapter=a, embed_query=_embed)
    assert got[0].text == "더 관련"


async def test_far_memories_are_cut_off():
    """자르지 않으면 질의와 무관한 기억이 매번 limit만큼 실린다."""
    a = _Adapter([_hit("p1", distance=0.99, text="무관")])
    s = _Session([("p1", "active", None, None)])
    assert await mr.recall(s, UID, query="q", adapter=a, embed_query=_embed) == []


def test_cutoff_sits_between_measured_relevant_and_irrelevant():
    """dev 실측: 관련 0.69~0.81, 무관 0.86~0.90. 경계가 그 사이에 있어야 한다."""
    assert 0.81 < mr.MAX_DISTANCE < 0.86
