"""published interaction contract 조회·발행 (6.3절).

계약은 **검색 성공 여부와 무관하게 항상 지켜야 하는 합의**다. 그래서 기억과 달리 회상
경로를 타지 않고 매 턴 프롬프트에 들어간다. 대신 **자주 바뀌지 않으므로** 안정 프리픽스에
둔다 — `prompt_assembly`도 CONTRACT를 STABLE로 분류한다.

정본은 locale-neutral `document_json`이고 `rendered_text`는 그 언어별 투영이다. 정본이
같으면(document_hash 동일) 새 version을 만들지 않는다.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import interaction_contract as ic

_PUBLISHED = text("""
SELECT rendered_text, document_json
FROM user_interaction_contracts
WHERE user_id = :user_id AND locale = :locale AND status = 'published'
LIMIT 1
""")

# 사용자·locale당 published는 정확히 하나다(부분 unique index가 강제).
_SUPERSEDE = text("""
UPDATE user_interaction_contracts SET status = 'superseded'
WHERE user_id = :user_id AND locale = :locale AND status = 'published'
""")

_PUBLISH = text("""
UPDATE user_interaction_contracts
SET status = 'published', published_at = now()
WHERE id = :contract_id AND user_id = :user_id AND status = 'draft'
RETURNING id
""")

_SAME_DOCUMENT = text("""
SELECT count(*) FROM user_interaction_contracts
WHERE user_id = :user_id AND status = 'published' AND render_hash = :hash
""")


async def published_text(
    session: AsyncSession, *, user_id: uuid.UUID, locale: str
) -> str:  # noqa: C901
    """프롬프트에 넣을 계약 블록. 없으면 빈 문자열이다.

    저장된 `rendered_text`를 그대로 쓰지 않고 **정본에서 다시 렌더한다** — 저장분이 옛
    template으로 만들어졌을 수 있고, 렌더 규칙(인용 슬롯)이 바뀌면 옛 문자열은 그 방어를
    받지 못한다.
    """
    row = (await session.execute(
        _PUBLISHED, {"user_id": user_id, "locale": locale}
    )).first()
    if row is None:
        return ""
    # ⚠️ jsonb는 드라이버가 **이미 파싱해서** 준다. json.loads를 걸면 TypeError가 나고,
    #    이건 챗 Phase 1이라 그 예외가 그대로 응답을 죽인다 — 계약이 생기는 순간 그
    #    사용자의 대화가 통째로 실패한다(실측).
    raw = row[1]
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            items = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return ""
    else:
        items = raw or []
    if not isinstance(items, list):
        return ""
    directives = []
    for it in items:
        try:
            directives.append(ic.Directive(
                kind=ic.Kind(it["kind"]),
                action=ic.Action(it["action"]),
                condition=ic.Condition(it["condition"]),
                polarity=ic.Polarity(it.get("polarity") or "positive"),
                target_tag=it.get("target_tag") or None,
                target_literal=it.get("target_literal") or None,
            ))
        except (KeyError, ValueError, ic.ContractViolation):
            # 손상된 항목 하나가 계약 전체를 날리지 않는다. 나머지는 지킨다.
            continue
    try:
        return ic.render_document(directives, language=locale)
    except ic.ContractViolation:
        return ""


async def publish(
    session: AsyncSession, *, contract_id: uuid.UUID, user_id: uuid.UUID, locale: str
) -> bool:
    """draft를 published로 올린다. 기존 published는 삭제하지 않고 superseded로 닫는다.

    지우면 변경 이력과 rollback이 사라진다(6.3절).
    """
    await session.execute(_SUPERSEDE, {"user_id": user_id, "locale": locale})
    row = (await session.execute(
        _PUBLISH, {"contract_id": contract_id, "user_id": user_id}
    )).first()
    return row is not None


async def has_same_document(
    session: AsyncSession, *, user_id: uuid.UUID, document_hash: str
) -> bool:
    """정본이 같으면 새 version을 만들지 않는다(6.3절)."""
    n = await session.scalar(_SAME_DOCUMENT, {"user_id": user_id, "hash": document_hash})
    return bool(n)
