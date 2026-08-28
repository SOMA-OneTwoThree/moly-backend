"""Conversation diary recall rendering contract."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from app.services import recall_diaries


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Session:
    async def scalar(self, stmt, params=None):
        return "승민"

    async def execute(self, stmt, params=None):
        return _Rows(
            [
                {
                    "id": uuid.UUID("22222222-2222-2222-2222-222222222222"),
                    "kind": "welcome",
                    "display_date": date(2026, 8, 4),
                    "title": "{유저이름}, 첫 만남",
                    "content": "오늘 {유저이름}과 처음 대화를 나눴다.",
                    "weather": "sunny",
                    "published_at": datetime(2026, 8, 4, tzinfo=timezone.utc),
                    "first_read_at": None,
                    "exact_count": 1,
                    "eligible_count": 1,
                    "content_match": True,
                }
            ]
        )


async def test_recall_renders_welcome_placeholder_at_egress() -> None:
    result = await recall_diaries.recall(
        _Session(),
        uuid.UUID("11111111-1111-1111-1111-111111111111"),
        query="첫 만남",
        need="full",
    )
    item = result["items"][0]
    assert item["title"] == "승민, 첫 만남"
    assert item["body"] == "오늘 승민과 처음 대화를 나눴다."
    # 전문을 줄 때는 body 한 곳에만 담는다. excerpt에 같은 본문을 또 넣으면 도구 결과 예산을
    # 두 배로 먹어 전문 2건만 요청해도 잘린다(실측 101자).
    assert item["excerpt"] is None
    assert "{유저이름}" not in item["body"]
    assert item["content_match"] is True


async def test_no_content_match_returns_recent_without_bodies() -> None:
    """질의에 맞은 일기가 없으면 본문을 주지 않는다.

    본문을 주면 모델이 그걸 물어본 일기인 양 읽고 지어낸다(dev 실측). 날짜·제목만 주면
    "이건가?" 하고 되물을 수는 있어도 없는 내용을 만들어 낼 수는 없다.
    """

    class _NoMatch(_Session):
        async def execute(self, stmt, params=None):
            rows = await super().execute(stmt, params)
            row = dict(rows._rows[0])
            row["content_match"] = False
            row["exact_count"] = 0
            return _Rows([row])

    result = await recall_diaries.recall(
        _NoMatch(),
        uuid.UUID("11111111-1111-1111-1111-111111111111"),
        query="있지도 않은 얘기",
        need="full",
    )
    assert result["status"] == "no_content_match"
    assert result["matched_count"] == 0
    assert result["returned_count"] == 1  # 빈손으로 돌아가지 않는다
    item = result["items"][0]
    assert item["body"] is None
    assert item["excerpt"] is None
    assert item["content_match"] is False
    assert item["display_date"]  # 날짜는 준다
    assert item["title"]  # 제목도 준다


async def test_query_without_match_is_not_a_filter() -> None:
    """맞지 않아도 후보가 사라지지 않는다. 예전에는 여기서 0건이 되어 '꺼낼 수 없다'가 나왔다."""

    class _NoMatch(_Session):
        async def execute(self, stmt, params=None):
            rows = await super().execute(stmt, params)
            row = dict(rows._rows[0])
            row["content_match"] = False
            row["exact_count"] = 0
            return _Rows([row])

    result = await recall_diaries.recall(
        _NoMatch(),
        uuid.UUID("11111111-1111-1111-1111-111111111111"),
        query="어제 일기",
        need="summary",
    )
    assert result["returned_count"] > 0


# --- SQL 자체를 고정한다 ---
#
# 위 테스트들은 session.execute를 통째로 가짜로 바꿔서 SQL을 한 줄도 실행하지 않는다.
# 그래서 _RECALL의 WHERE 필터를 되돌려도, ORDER BY를 되돌려도 전부 통과한다.
# 원래 결함(질의가 하드 필터라 "어제 일기"에 0건)이 정확히 SQL에 있었으므로 여기서 고정한다.


def _sql() -> str:
    return " ".join(str(recall_diaries._RECALL).split())


def test_query_is_not_a_filter_in_sql() -> None:
    """질의로 후보를 걸러내면 안 된다. 순위만 정한다.

    `eligible` 단계의 WHERE는 소유·공개 여부라 정상이다. `ranked` 단계에 WHERE가 생기면
    그게 곧 질의 필터이고, "어제 일기" 같은 말에 0건이 되어 캐피가 일기를 지어낸다.

    #15c 이후: 절단은 `top`의 두 갈래(맞은 행 점수순 / 안 맞은 행 최신순)가 나눠 하므로,
    안 맞은 행도 반드시 도달 가능해야 한다 — WHERE NOT content_match 갈래의 존재가 그 증거다.
    """
    ranked = _sql().split("ranked AS (", 1)[1].split("), counts AS (", 1)[0]
    assert " WHERE " not in ranked.upper(), (
        "ranked 단계에 WHERE가 생겼다 — 질의가 다시 하드 필터가 됐다"
    )
    sql = _sql()
    assert "WHERE content_match" in sql and "WHERE NOT content_match" in sql, (
        "안 맞은 행의 자리 채움 갈래가 사라졌다 — 질의가 하드 필터가 됐다"
    )
    assert "NOT content_match ORDER BY display_date DESC" in sql, (
        "자리 채움은 벡터 점수가 아니라 display_date DESC 상한이어야 한다(#15c)"
    )


def test_sql_marks_each_row_with_content_match() -> None:
    """행마다 '진짜로 맞았는지'가 있어야 안 맞은 행의 본문을 뺄 수 있다."""
    assert "content_match" in _sql()


def test_matched_count_counts_only_real_matches() -> None:
    """맞은 수와 전체 후보 수는 다른 값이다."""
    sql = _sql()
    assert "FILTER (WHERE content_match)" in sql, "맞은 수를 따로 세지 않는다"
    assert "AS exact_count" in sql and "AS eligible_count" in sql


def test_matched_rows_come_first() -> None:
    """맞은 것이 먼저 나와야 limit 안에 답이 들어온다.

    #15c 이후 ORDER BY가 여러 개(top 갈래 내부 정렬)라 **최종** ORDER BY를 본다."""
    sql = _sql()
    order = sql.rsplit("ORDER BY", 1)[1]
    assert order.strip().startswith("t.content_match DESC"), (
        "맞은 행을 먼저 정렬하지 않는다 — 후보가 많으면 답이 limit 밖으로 밀린다"
    )


async def test_unmatched_row_gets_no_body_even_when_another_matched() -> None:
    """한 건이 맞았다고 해서 안 맞은 일기 본문까지 실으면 안 된다.

    자리를 채우려고 딸려온 일기인데 모델은 그걸 물어본 일기로 읽는다. 실제로 이 경로로
    캐피가 엉뚱한 일기 내용을 답한 적이 있다.
    """

    class _Mixed(_Session):
        async def execute(self, stmt, params=None):
            rows = await super().execute(stmt, params)
            base = dict(rows._rows[0])
            hit = dict(base, content_match=True, exact_count=1, eligible_count=2)
            miss = dict(
                base,
                id=uuid.UUID("33333333-3333-3333-3333-333333333333"),
                content_match=False,
                exact_count=1,
                eligible_count=2,
            )
            return _Rows([hit, miss])

    result = await recall_diaries.recall(
        _Mixed(),
        uuid.UUID("11111111-1111-1111-1111-111111111111"),
        query="첫 만남",
        need="full",
    )
    assert result["status"] == "ok"
    assert result["matched_count"] == 1
    hit, miss = result["items"]
    assert hit["content_match"] is True and hit["body"]
    assert miss["content_match"] is False
    assert miss["body"] is None and miss["excerpt"] is None, "안 맞은 일기 본문이 나갔다"
    assert miss["display_date"] and miss["title"]  # 날짜·제목은 준다
