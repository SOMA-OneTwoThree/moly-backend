"""관계 프로필(W9)의 **저장소** — SQL과 publish 트랜잭션 불변식. 문서·렌더는 `relationship_profile`.

여기서 지키는 것(어기면 조용히 깨진다):

1. **locale당 published 1개.** 마지막 방어선은 부분 유니크 인덱스
   `relationship_profiles_one_published_idx`다. 여기 코드는 기존 published를 `FOR UPDATE`로 잠그고
   **superseded로 바꾼 뒤** draft를 published로 올리며, 두 UPDATE 사이에 commit하지 않는다.
   락만 믿지 않는다 — 인덱스가 없으면 동시 publish에서 두 행이 함께 살아남는다.
2. **terminal 불가역.** `invalidated`/`superseded`를 published로 되돌리는 UPDATE가 하나도 없다.
   publish는 `WHERE status='draft'`, supersede는 `WHERE status='published'`만 건드리고
   0행이면 되살리지 않고 실패시킨다.
3. **cross-user 차단.** edge는 복합 FK가 스키마에서 막고, 코드는 근거 검증 쿼리를 전부
   `user_id=:user_id`로 스코프한다. 타 유저 fact/insight id는 "없는 근거"가 되어 publish가 죽는다.
4. **근거 재대조.** publish 시 document_json의 ref와 edge 행이 `type/id/item_key`까지 양방향으로
   같아야 하고, 모든 근거가 지금 active이며 forget marker에 걸리지 않아야 한다. 렌더 경로도 매번
   같은 검사를 다시 해 무효 근거 항목을 빼고 렌더한다(재생성 지연 중 잊은 내용 노출 방지).
5. **같은 내용 재발행 금지.** `render_hash`가 기존 published와 같으면 draft를 만들지도, publish하지도
   않는다. 안정 프리픽스가 매 턴 바뀌면 프롬프트 캐시가 깨져 비용이 폭증한다.

커밋하지 않는다 — 전부 호출측(잡 finalize) 트랜잭션 경계에 합류한다.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import i18n
from app.services import relationship_profile as rp

# 잡 finalize 사유 코드(계약). publish 실패는 재시도로 풀리지 않으므로 dead/invalid_provenance다.
RESULT_PUBLISHED = "published"
RESULT_UNCHANGED = "unchanged"
RESULT_STALE_GENERATION = "stale_generation"
RESULT_STALE_PROFILE_INPUT = "stale_profile_input"
RESULT_INVALID_PROVENANCE = "invalid_provenance"

STATUS_DRAFT = "draft"
STATUS_PUBLISHED = "published"
STATUS_INVALIDATED = "invalidated"
STATUS_SUPERSEDED = "superseded"


class ProfileRepoError(Exception):
    """저장소 불변식 위반 — 부분 반영 없이 트랜잭션을 실패시킨다."""


class InvalidProvenanceError(ProfileRepoError):
    """근거가 문서와 어긋나거나 더 이상 유효하지 않다. publish 없이 `dead/invalid_provenance`."""


class TerminalProfileError(ProfileRepoError):
    """terminal(또는 이미 바뀐) 행을 되살리려 했다. 되돌리지 않고 실패시킨다."""


@dataclass(frozen=True, slots=True)
class DraftResult:
    """draft 생성 결과. `status='unchanged'`면 행을 만들지 않았고 profile_id도 없다."""

    status: str
    profile_id: uuid.UUID | None
    version: int | None
    render: rp.RenderResult


@dataclass(frozen=True, slots=True)
class PublishResult:
    status: str
    profile_id: uuid.UUID | None = None
    version: int | None = None
    superseded_id: uuid.UUID | None = None


# ─────────────────────────────────────────────────────────────
# 읽기
# ─────────────────────────────────────────────────────────────
# draft가 스탬프할 입력 좌표. publish가 같은 값을 **FOR UPDATE로 다시 읽어** 비교한다.
_CONTEXT_SQL = text("""
SELECT memory_generation, relationship_profile_input_revision
FROM chat_contexts WHERE user_id=:user_id
""")

_LOCK_CONTEXT_SQL = text("""
SELECT memory_generation, relationship_profile_input_revision
FROM chat_contexts WHERE user_id=:user_id FOR UPDATE
""")

_PUBLISHED_SQL = text("""
SELECT id, version, document_json, rendered_text, render_hash
FROM relationship_profiles
WHERE user_id=:user_id AND locale=:locale AND status='published'
""")

_LOCK_PUBLISHED_SQL = text("""
SELECT id, version, render_hash
FROM relationship_profiles
WHERE user_id=:user_id AND locale=:locale AND status='published'
FOR UPDATE
""")

_DRAFT_SQL = text("""
SELECT id, version, locale, memory_generation, relationship_profile_input_revision,
       document_json, render_hash, status
FROM relationship_profiles WHERE user_id=:user_id AND id=:profile_id
""")

_NEXT_VERSION_SQL = text("""
SELECT COALESCE(max(version), 0) + 1 FROM relationship_profiles
WHERE user_id=:user_id AND locale=:locale
""")

_EDGES_SQL = text("""
SELECT item_key, fact_id, insight_id FROM relationship_profile_sources
WHERE user_id=:user_id AND relationship_profile_id=:profile_id
""")

# 지금 근거로 쓸 수 있는 fact — 같은 유저 + active + 어떤 forget marker에도 안 걸림.
# marker 비교는 fact가 저장해 둔 (normalization_version, content_hash)와 marker의
# (normalization_version, normalized_hash)를 그대로 맞춘다(W8 공용 해시의 두 이름).
_USABLE_FACT_CLAUSE = """
  f.status='active'
  AND NOT EXISTS (
    SELECT 1 FROM memory_forget_markers m
    WHERE m.user_id = f.user_id AND (
      m.scope='all'
      OR (m.scope='predicate' AND f.predicate IS NOT NULL AND m.predicate = f.predicate)
      OR (m.scope='fact' AND m.normalization_version = f.normalization_version
                         AND m.normalized_hash = f.content_hash)
    )
  )
"""

_USABLE_FACTS_SQL = text(f"""
SELECT f.id FROM memory_facts f
WHERE f.user_id=:user_id AND f.id IN :ids AND {_USABLE_FACT_CLAUSE}
""").bindparams(bindparam("ids", expanding=True))

# insight는 자신이 active인 것으로 부족하다 — **모든** source fact가 위 조건을 만족해야 한다.
# 하나라도 잊혔거나 대체됐으면 그 통찰은 더 이상 근거가 없다.
_USABLE_INSIGHTS_SQL = text(f"""
SELECT i.id FROM memory_insights i
WHERE i.user_id=:user_id AND i.id IN :ids AND i.status='active'
  AND NOT EXISTS (
    SELECT 1 FROM memory_insight_sources s
    WHERE s.user_id = i.user_id AND s.insight_id = i.id
      AND NOT EXISTS (
        SELECT 1 FROM memory_facts f
        WHERE f.user_id = s.user_id AND f.id = s.fact_id AND {_USABLE_FACT_CLAUSE}
      )
  )
""").bindparams(bindparam("ids", expanding=True))


# ─────────────────────────────────────────────────────────────
# 쓰기 — 상태 변경 UPDATE는 전부 출발 상태를 WHERE에 못 박는다(terminal 불가역).
# ─────────────────────────────────────────────────────────────
_INSERT_DRAFT_SQL = text("""
INSERT INTO relationship_profiles
  (user_id, version, locale, memory_generation, relationship_profile_input_revision,
   document_json, rendered_text, render_hash, status)
VALUES
  (:user_id, :version, :locale, :memory_generation, :input_revision,
   CAST(:document_json AS jsonb), :rendered_text, :render_hash, 'draft')
RETURNING id
""")

_INSERT_EDGE_SQL = text("""
INSERT INTO relationship_profile_sources
  (user_id, relationship_profile_id, item_key, fact_id, insight_id)
VALUES (:user_id, :profile_id, :item_key, :fact_id, :insight_id)
""")

_SUPERSEDE_PUBLISHED_SQL = text("""
UPDATE relationship_profiles SET status='superseded'
WHERE id=:profile_id AND user_id=:user_id AND status='published'
RETURNING id
""")

_PUBLISH_DRAFT_SQL = text("""
UPDATE relationship_profiles SET status='published', published_at=clock_timestamp()
WHERE id=:profile_id AND user_id=:user_id AND status='draft'
RETURNING id
""")

_INVALIDATE_DRAFT_SQL = text("""
UPDATE relationship_profiles SET status='invalidated'
WHERE id=:profile_id AND user_id=:user_id AND status='draft'
RETURNING id
""")


async def input_coordinates(session: AsyncSession, user_id: uuid.UUID | str) -> tuple[int, int]:
    """draft가 스탬프할 `(memory_generation, relationship_profile_input_revision)`.

    행이 없으면 (0, 0) — 아직 대화 컨텍스트가 없는 유저(기억 변경이 한 번도 없었다는 뜻)다.
    """
    row = (await session.execute(_CONTEXT_SQL, {"user_id": user_id})).first()
    return (int(row[0]), int(row[1])) if row is not None else (0, 0)


async def published_profile(
    session: AsyncSession, user_id: uuid.UUID | str, locale: str
) -> dict | None:
    row = (
        await session.execute(_PUBLISHED_SQL, {"user_id": user_id, "locale": locale})
    ).mappings().first()
    return dict(row) if row is not None else None


async def _usable_sources(
    session: AsyncSession,
    user_id: uuid.UUID | str,
    fact_ids: Sequence[str],
    insight_ids: Sequence[str],
) -> tuple[frozenset[str], frozenset[str]]:
    """지금 근거로 쓸 수 있는 id 집합. 요청했는데 안 돌아온 id는 무효(타 유저·terminal·forget)."""
    facts: set[str] = set()
    insights: set[str] = set()
    if fact_ids:
        rows = await session.execute(
            _USABLE_FACTS_SQL,
            {"user_id": user_id, "ids": [rp.as_uuid(i) for i in dict.fromkeys(fact_ids)]},
        )
        facts = {str(r[0]) for r in rows.all()}
    if insight_ids:
        rows = await session.execute(
            _USABLE_INSIGHTS_SQL,
            {"user_id": user_id, "ids": [rp.as_uuid(i) for i in dict.fromkeys(insight_ids)]},
        )
        insights = {str(r[0]) for r in rows.all()}
    return frozenset(facts), frozenset(insights)


# ─────────────────────────────────────────────────────────────
# draft 작성
# ─────────────────────────────────────────────────────────────
async def create_draft(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    language: str | None,
    document: rp.ProfileDocument,
    nickname: str | None = None,
) -> DraftResult:
    """문서를 렌더해 draft 행 + edge를 쓴다(커밋하지 않는다).

    기존 published와 `render_hash`가 같으면 **행을 만들지 않고** `unchanged`로 끝낸다 — 같은 내용을
    새 version으로 다시 올리면 안정 프리픽스가 교체돼 프롬프트 캐시가 통째로 날아간다.
    """
    locale = i18n.resolve(language)
    masked = rp.mask_document(document, nickname)  # 저장 직전 마스킹 강제(자연 멱등)
    rendered = rp.render(masked, locale)

    generation, revision = await input_coordinates(session, user_id)
    current = await published_profile(session, user_id, locale)
    if current is not None and current["render_hash"] == rendered.render_hash:
        return DraftResult(status=RESULT_UNCHANGED, profile_id=None, version=None, render=rendered)

    version = int(
        (await session.execute(_NEXT_VERSION_SQL, {"user_id": user_id, "locale": locale})).scalar_one()
    )
    row = (
        await session.execute(
            _INSERT_DRAFT_SQL,
            {
                "user_id": user_id,
                "version": version,
                "locale": locale,
                "memory_generation": generation,
                "input_revision": revision,
                "document_json": json.dumps(rendered.document.as_json(), ensure_ascii=False),
                "rendered_text": rendered.text,
                "render_hash": rendered.render_hash,
            },
        )
    ).first()
    profile_id = row[0]
    await _insert_edges(session, user_id=user_id, profile_id=profile_id, document=rendered.document)
    return DraftResult(
        status=STATUS_DRAFT, profile_id=profile_id, version=version, render=rendered
    )


async def _insert_edges(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    profile_id: uuid.UUID,
    document: rp.ProfileDocument,
) -> int:
    """document_json의 ref를 **같은 트랜잭션에서** edge 행으로 쓴다(양쪽이 어긋나면 publish가 죽는다)."""
    rows = [
        {
            "user_id": user_id,
            "profile_id": profile_id,
            "item_key": item.item_key,
            "fact_id": rp.as_uuid(ref.id) if ref.type == rp.SOURCE_FACT else None,
            "insight_id": rp.as_uuid(ref.id) if ref.type == rp.SOURCE_INSIGHT else None,
        }
        for item in document.items
        for ref in item.source_refs
    ]
    if rows:
        await session.execute(_INSERT_EDGE_SQL, rows)
    return len(rows)


# ─────────────────────────────────────────────────────────────
# publish — 잠그고, 대조하고, superseded → published를 한 트랜잭션에서.
# ─────────────────────────────────────────────────────────────
async def publish(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    profile_id: uuid.UUID,
    locale: str | None = None,
) -> PublishResult:
    """draft를 published로 올린다(커밋하지 않는다 — 잡 finalize와 같은 트랜잭션).

    순서가 계약이다: chat_contexts 잠금 → 현 published 잠금 → 근거 양방향 대조 → generation/revision
    대조 → 기존 superseded → draft published. 두 UPDATE 사이에 commit하지 않는다.
    """
    draft = (
        await session.execute(_DRAFT_SQL, {"user_id": user_id, "profile_id": profile_id})
    ).mappings().first()
    if draft is None:
        # 타 유저 프로필이거나 없는 id — 근거 문제와 같은 등급으로 다룬다(재시도해도 안 풀린다).
        raise InvalidProvenanceError(f"draft를 찾을 수 없다: profile_id={profile_id}")
    if draft["status"] != STATUS_DRAFT:
        raise TerminalProfileError(
            f"draft가 아닌 프로필은 publish할 수 없다: status={draft['status']!r}"
        )
    draft_locale = draft["locale"]
    if locale is not None and locale != draft_locale:
        raise InvalidProvenanceError(f"locale 불일치: 요청={locale!r} draft={draft_locale!r}")

    # 1) 입력 좌표를 잠근 채로 읽는다(그 사이 새 기억이 커밋되지 않게).
    row = (await session.execute(_LOCK_CONTEXT_SQL, {"user_id": user_id})).first()
    generation, revision = (int(row[0]), int(row[1])) if row is not None else (0, 0)

    # 2) 같은 locale의 현 published도 잠근다(교체 대상이 도중에 바뀌지 않게).
    current = (
        await session.execute(_LOCK_PUBLISHED_SQL, {"user_id": user_id, "locale": draft_locale})
    ).mappings().first()

    # 3) 근거 대조 — JSON ↔ edge 양방향 + 지금도 유효한 근거인지.
    document = rp.document_from_json(draft["document_json"])
    await _assert_provenance(session, user_id=user_id, profile_id=profile_id, document=document)

    # 4) 입력 좌표 대조 — 어긋나면 결과를 버린다(publish도, 재시도도 하지 않는다).
    if int(draft["memory_generation"]) != generation:
        await _invalidate(session, user_id=user_id, profile_id=profile_id)
        return PublishResult(status=RESULT_STALE_GENERATION)
    if int(draft["relationship_profile_input_revision"]) != revision:
        await _invalidate(session, user_id=user_id, profile_id=profile_id)
        return PublishResult(status=RESULT_STALE_PROFILE_INPUT)

    # 5) 내용이 같으면 새 published를 만들지 않는다(캐시 유지).
    if current is not None and current["render_hash"] == draft["render_hash"]:
        await _invalidate(session, user_id=user_id, profile_id=profile_id)
        return PublishResult(
            status=RESULT_UNCHANGED, profile_id=current["id"], version=int(current["version"])
        )

    # 6) 기존을 먼저 닫고 draft를 올린다. 사이에 commit 없음 → locale당 published가 2개인 순간이 없다.
    superseded_id = None
    if current is not None:
        closed = (
            await session.execute(
                _SUPERSEDE_PUBLISHED_SQL, {"profile_id": current["id"], "user_id": user_id}
            )
        ).first()
        if closed is None:  # FOR UPDATE로 잡고 있었는데 published가 아니게 됐다 = 불변식 붕괴
            raise TerminalProfileError(f"published가 아닌 프로필을 대체할 수 없다: {current['id']}")
        superseded_id = closed[0]
    published = (
        await session.execute(_PUBLISH_DRAFT_SQL, {"profile_id": profile_id, "user_id": user_id})
    ).first()
    if published is None:  # 그 사이 invalidated/superseded로 갔다 → 되살리지 않는다
        raise TerminalProfileError(f"draft가 아닌 프로필을 publish할 수 없다: {profile_id}")
    return PublishResult(
        status=RESULT_PUBLISHED,
        profile_id=published[0],
        version=int(draft["version"]),
        superseded_id=superseded_id,
    )


async def _invalidate(
    session: AsyncSession, *, user_id: uuid.UUID | str, profile_id: uuid.UUID
) -> None:
    """폐기된 draft를 닫는다. draft가 아니면 아무것도 바꾸지 않는다(terminal 불가역)."""
    await session.execute(_INVALIDATE_DRAFT_SQL, {"profile_id": profile_id, "user_id": user_id})


async def _assert_provenance(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    profile_id: uuid.UUID,
    document: rp.ProfileDocument,
) -> None:
    """JSON ref ↔ edge 행 양방향 일치 + 모든 근거가 같은 유저의 유효한 근거인지."""
    rows = (
        await session.execute(_EDGES_SQL, {"user_id": user_id, "profile_id": profile_id})
    ).mappings().all()
    edges = {
        (
            r["item_key"],
            rp.SOURCE_FACT if r["fact_id"] is not None else rp.SOURCE_INSIGHT,
            str(r["fact_id"] if r["fact_id"] is not None else r["insight_id"]),
        )
        for r in rows
    }
    expected = document.edges()
    if edges != expected:
        raise InvalidProvenanceError(
            f"document_json과 edge가 다르다 (누락={sorted(expected - edges)}, "
            f"초과={sorted(edges - expected)})"
        )

    fact_ids, insight_ids = rp.source_ids(document)
    usable_facts, usable_insights = await _usable_sources(
        session, user_id, sorted(fact_ids), sorted(insight_ids)
    )
    dead_facts = fact_ids - usable_facts
    dead_insights = insight_ids - usable_insights
    if dead_facts or dead_insights:
        raise InvalidProvenanceError(
            f"더 이상 유효하지 않은 근거 (fact={sorted(dead_facts)}, insight={sorted(dead_insights)})"
        )


# ─────────────────────────────────────────────────────────────
# 렌더 경로 — 프롬프트에 실을 문자열. **매번** 근거를 다시 대조한다.
# ─────────────────────────────────────────────────────────────
async def prompt_text(
    session: AsyncSession, *, user_id: uuid.UUID | str, language: str | None
) -> str:
    """published 프로필 → 프롬프트 문자열. 무효 근거를 가진 항목은 빼고 다시 렌더한다.

    저장된 `rendered_text`를 그대로 쓰지 않는다 — 재생성이 밀린 동안 forget/supersede된 근거가
    그 문자열에 남아 있을 수 있다. 근거가 전부 무효면 빈 문자열이며 다른 저장소로 폴백하지 않는다.
    문자열은 저장 형태(`{유저이름}` placeholder) 그대로다. 현재 이름 치환은 주입 직전에 한다.
    """
    locale = i18n.resolve(language)
    current = await published_profile(session, user_id, locale)
    if current is None:
        return ""
    document = rp.document_from_json(current["document_json"])
    fact_ids, insight_ids = rp.source_ids(document)
    usable_facts, usable_insights = await _usable_sources(
        session, user_id, sorted(fact_ids), sorted(insight_ids)
    )
    filtered = rp.filter_by_valid_sources(
        document, valid_fact_ids=usable_facts, valid_insight_ids=usable_insights
    )
    if filtered.is_empty:
        return ""
    return rp.render(filtered, locale).text
