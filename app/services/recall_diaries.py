"""Answer-complete diary recall used by the agent and Dev diagnostics."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.profile import Profile
from app.services.account import _uid
from app.services import naming, privacy

MAX_RETURNED = 5

_RECALL = text("""
WITH params AS (
  SELECT CAST(:embedding AS vector(1536)) AS embedding,
         CAST(:query AS text) AS query,
         CAST(:from_date AS date) AS from_date,
         CAST(:to_date AS date) AS to_date,
         CAST(:focus_id AS uuid) AS focus_id
), eligible AS (
  SELECT d.id,d.kind,d.display_date,d.title,d.content,d.weather,d.published_at,d.first_read_at,
         rd.search_text,
         CASE WHEN p.embedding IS NULL OR rd.embedding IS NULL THEN NULL
              ELSE 1-(rd.embedding <=> p.embedding) END AS similarity
  FROM diaries d
  JOIN diary_recall_documents rd ON rd.user_id=d.user_id AND rd.diary_id=d.id
  CROSS JOIN params p
  WHERE d.user_id=:user_id AND d.record_status='published' AND d.deleted_at IS NULL
    AND d.published_at IS NOT NULL AND d.published_at<=:now
    AND d.kind IN ('welcome','shared_day','capi_day')
    AND (p.from_date IS NULL OR d.display_date>=p.from_date)
    AND (p.to_date IS NULL OR d.display_date<=p.to_date)
    AND (p.focus_id IS NULL OR d.id=p.focus_id)
), ranked AS (
  -- 질의는 **거르는 조건이 아니라 순위 신호**다. 예전에는 여기서 걸러냈는데, 그러면
  -- "어제 일기 보여줘"처럼 내용이 아닌 말이 들어왔을 때 0건이 되어 캐피가 "꺼낼 수 없다"고
  -- 답하고 다음 턴에 내용을 지어냈다(dev 실측). 이제 전부 통과시키고 순서만 정한다.
  --
  -- `content_match`는 **질의에 진짜로 맞았는지**를 행마다 표시한다. 호출한 쪽이 이 값을 보고
  -- 맞은 게 하나도 없으면 본문을 빼고 돌려준다 — 인용할 본문이 없으면 지어낼 수도 없다.
  SELECT *,
    CASE WHEN p.query IS NULL THEN 1.0
         WHEN search_text ILIKE ('%' || p.query || '%') THEN 1.0
         ELSE COALESCE(similarity,0.0) END AS score,
    (p.query IS NULL
     OR search_text ILIKE ('%' || p.query || '%')
     OR COALESCE(similarity,0.0)>=:min_similarity) AS content_match
  FROM eligible CROSS JOIN params p
)
SELECT *,
       count(*) FILTER (WHERE content_match) OVER() AS exact_count,
       count(*) OVER() AS eligible_count
FROM ranked
-- 맞은 것을 먼저. 그다음 점수, 그다음 최신순.
ORDER BY content_match DESC,score DESC,display_date DESC,id DESC
LIMIT :limit
""")


def _vector_literal(vector: list[float] | None) -> str | None:
    if vector is None:
        return None
    return "[" + ",".join(format(float(value), ".9g") for value in vector) + "]"


async def recall(
    session: AsyncSession,
    user_id: str | uuid.UUID,
    *,
    query: str | None,
    need: str = "summary",
    from_date: date | None = None,
    to_date: date | None = None,
    focus_id: uuid.UUID | None = None,
    limit: int = 3,
    query_embedding: list[float] | None = None,
) -> dict[str, Any]:
    uid = _uid(str(user_id)) if not isinstance(user_id, uuid.UUID) else user_id
    await privacy.ensure_subject_active(session, uid)
    if from_date and to_date and from_date > to_date:
        raise ValueError("from_date must be <= to_date")
    if focus_id is not None:
        query = None
    effective_limit = max(1, min(limit, MAX_RETURNED))
    nickname = await session.scalar(select(Profile.nickname).where(Profile.id == uid))
    rows = (
        await session.execute(
            _RECALL,
            {
                "user_id": uid,
                "query": query.strip() if query else None,
                "embedding": _vector_literal(query_embedding),
                "min_similarity": 0.25,
                "from_date": from_date,
                "to_date": to_date,
                "focus_id": focus_id,
                "now": datetime.now(timezone.utc),
                "limit": effective_limit,
            },
        )
    ).mappings().all()
    exact_count = int(rows[0]["exact_count"]) if rows else 0
    include_body = need in {"full", "full_card", "quote"}
    # 질의를 줬는데 맞은 게 하나도 없으면, 돌려주는 건 "그 질의의 답"이 아니라 그냥 최근 일기다.
    # 일기가 아예 없어서 빈 목록인 경우와 구분한다 — 그건 "안 맞은" 게 아니라 "없는" 것이다.
    fallback = bool(query) and exact_count == 0 and bool(rows)
    items = []
    for row in rows:
        body = naming.render(str(row["content"] or ""), nickname)
        title = naming.render(str(row["title"]), nickname) if row["title"] else None
        matched = bool(row["content_match"])
        # ⚠️ 본문 유무는 **요청 단위가 아니라 행 단위**로 정한다. 한 건이라도 맞으면 나머지
        # 안 맞은 행까지 본문이 실리던 문제가 있었다 — 자리를 채우려고 딸려온 일기인데
        # 모델은 그걸 물어본 일기로 읽는다. 안 맞은 행은 날짜·제목만 준다.
        # 본문 없이 줄 때도 제목은 남긴다. 날짜와 제목만으로 "이건가?" 하고 되물을 수 있다.
        if fallback or not matched:
            excerpt, full_body = None, None
        elif include_body:
            # excerpt와 body에 같은 본문을 두 번 담으면 도구 결과 예산(600토큰)을 두 배로 먹어
            # 전문 2건만 요청해도 잘린다(실측 101자). 전문을 줄 때는 body 한 곳에만 담는다.
            excerpt, full_body = None, body
        else:
            excerpt, full_body = body[:400], None
        items.append(
            {
                "ref": {"type": "diary", "id": str(row["id"])},
                "id": str(row["id"]),
                "kind": row["kind"],
                "display_date": row["display_date"].isoformat(),
                "title": title,
                "excerpt": excerpt,
                "body": full_body,
                "weather": row["weather"],
                "read": row["first_read_at"] is not None,
                "content_match": matched,
            }
        )
    return {
        # 질의에 맞은 게 없으면 그 사실을 상태로 알린다. 모델이 최근 일기를 답인 양 내놓지 않게.
        "status": "no_content_match" if fallback else "ok",
        "matched_count": exact_count,
        "returned_count": len(items),
        "coverage": "complete" if exact_count <= effective_limit else "partial",
        "has_more": exact_count > effective_limit,
        "items": items,
    }
