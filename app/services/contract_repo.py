"""published interaction contract 조회·발행 (6.3절).

계약은 **검색 성공 여부와 무관하게 항상 지켜야 하는 합의**다. 그래서 기억과 달리 회상
경로를 타지 않고 매 턴 프롬프트에 들어간다. 대신 **자주 바뀌지 않으므로** 안정 프리픽스에
둔다 — 실제 조립은 chat.py의 안정 프리픽스에 포함한다.

정본은 locale-neutral `document_json`이고 `rendered_text`는 그 언어별 투영이다. 정본이
같으면(document_hash 동일) 새 version을 만들지 않는다.
"""
from __future__ import annotations

import json
import hashlib
import uuid
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import interaction_contract as ic
from app.services.contract_compiler import (
    conversation_scope_end_applies, conversation_scope_end_candidate, conversation_scope_end_targets,
    conversation_scope_end_position,
)
from app.config import settings

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

_RETENTION_DOCUMENT = text("""
SELECT c.document_json FROM user_interaction_contracts c
JOIN chat_contexts cc ON cc.user_id=c.user_id
WHERE c.user_id=:user_id AND c.status='published'
  AND cc.memory_generation=:generation AND cc.contract_generation=:generation
ORDER BY c.published_at DESC,c.version DESC,c.id LIMIT 1
""")

_VALID_SOURCES = text("""
SELECT id,content FROM messages
WHERE user_id=:user_id AND id=ANY(CAST(:ids AS bigint[]))
  AND sender='user' AND kind='normal'
""")

_PENDING_CONVERSATION_ENDS = text("""
SELECT m.id,m.content FROM messages m JOIN chat_contexts cc ON cc.user_id=m.user_id
WHERE m.user_id=:user_id AND cc.memory_generation=:generation
  AND m.sender='user' AND m.kind='normal'
  AND m.id>cc.contract_processed_through_message_id
  AND m.id>:after
ORDER BY m.id LIMIT 200
""")


def active_retained_items(items: list[dict], *, activity_date: date | None = None) -> list[dict]:
    return [item for item in items if item.get("status", "active") == "active"
            and (item.get("scope", "persistent") != "day"
                 or (activity_date is not None and item.get("activity_date") == str(activity_date)))]


def _scope_metadata(item: dict) -> dict:
    value = dict(item)
    key = value.get("item_key")
    if not isinstance(key, str) or not key or len(key) > 300:
        raise ic.ContractViolation("계약 항목 키가 잘못됐다")
    scope = value.get("scope", "persistent")
    status = value.get("status", "active")
    if scope not in {"persistent", "conversation", "day"} or status not in {"active", "ended"}:
        raise ic.ContractViolation("계약 적용 범위·상태가 잘못됐다")
    value["scope"], value["status"] = scope, status
    if scope == "conversation":
        start = value.get("scope_start_message_id", value.get("source_message_id"))
        if type(start) is not int or start != value.get("source_message_id"):
            raise ic.ContractViolation("대화 선호의 시작 근거가 잘못됐다")
        value["scope_start_message_id"] = start
        end = value.get("scope_end")
        if status == "ended":
            if not isinstance(end, dict) or set(end) != {"source_message_id", "source_hash", "start_utf8", "end_utf8"}:
                raise ic.ContractViolation("대화 선호 종료의 원문 근거가 없다")
            if type(end["source_message_id"]) is not int or end["source_message_id"] < start:
                raise ic.ContractViolation("대화 선호 종료가 시작보다 앞선다")
        elif end is not None:
            raise ic.ContractViolation("적용 중인 대화 선호에 종료 근거가 섞였다")
    elif status != "active" or value.get("scope_end") is not None:
        raise ic.ContractViolation("대화 범위 외의 선호를 대화 종료로 끝낼 수 없다")
    if scope == "day":
        try:
            date.fromisoformat(value["activity_date"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ic.ContractViolation("당일 선호의 활동일이 잘못됐다") from exc
    return value


def _validate_reference(ref: dict, sources: dict[int, str]) -> str:
    body = sources.get(ref["source_message_id"])
    if body is None or hashlib.sha256(body.encode()).hexdigest() != ref.get("source_hash"):
        raise ic.ContractViolation("계약 원문 근거가 바뀌었거나 제외됐다")
    start, end = ref.get("start_utf8"), ref.get("end_utf8")
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(body.encode()):
        raise ic.ContractViolation("계약 원문 구간이 잘못됐다")
    try:
        return body.encode()[start:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ic.ContractViolation("계약 근거가 UTF-8 경계를 자른다") from exc


def decode_document(raw) -> list[dict]:
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ic.ContractViolation("계약 JSON 형식이 잘못됐다") from exc
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise ic.ContractViolation("계약 문서 형식이 잘못됐다")
    return raw


def directive_from_item(item: dict) -> ic.Directive:
    try:
        return ic.validate(ic.Directive(
            kind=ic.Kind(item["kind"]), action=ic.Action(item["action"]),
            condition=ic.Condition(item["condition"]),
            polarity=ic.Polarity(item.get("polarity") or "positive"),
            target_tag=item.get("target_tag") or None,
            target_literal=item.get("target_literal") or None,
        ))
    except (KeyError, ValueError, TypeError) as exc:
        raise ic.ContractViolation("계약 항목 내용이 잘못됐다") from exc


async def retained_document(session: AsyncSession, *, user_id: uuid.UUID,
                            generation: int) -> list[dict]:
    """현 세대의 원문 검증본만 반환. 손상/삭제/재분류를 빈 선호로 숨기지 않는다."""
    row = (await session.execute(_RETENTION_DOCUMENT, {
        "user_id": user_id, "generation": generation,
    })).first()
    if row is None:
        return []
    items = decode_document(row[0])
    if not items:
        return []
    if any(item.get("generation") != generation for item in items):
        # v2 최초 이관은 원문 처음부터 다시 처리한다. 구형 문서를 정본 승격하지 않는다.
        return []
    return await validate_retained_items(session, user_id=user_id, generation=generation, items=items)


async def validate_retained_items(session: AsyncSession, *, user_id: uuid.UUID,
                                  generation: int, items: list[dict]) -> list[dict]:
    """분할 작업의 미게시 draft도 원본 ID·구간·전체 지문으로 검증한다."""
    if not items:
        return []
    if any(type(item.get("generation")) is not int or item["generation"] != generation for item in items):
        raise ic.ContractViolation("계약 draft의 기억 세대가 바뀌었다")
    items = [_scope_metadata(item) for item in items]
    if len({item["item_key"] for item in items}) != len(items):
        raise ic.ContractViolation("계약 항목 키가 중복됐다")
    ids = [item.get("source_message_id") for item in items]
    ids.extend(item["scope_end"]["source_message_id"] for item in items if item.get("status") == "ended")
    if any(type(value) is not int or value <= 0 for value in ids):
        raise ic.ContractViolation("계약 근거 메시지 ID가 잘못됐다")
    sources = {row[0]: row[1] or "" for row in (await session.execute(
        _VALID_SOURCES, {"user_id": user_id, "ids": ids},
    )).all()}
    for item in items:
        directive_from_item(item)
        _validate_reference(item, sources)
        if item.get("status") == "ended":
            end = item["scope_end"]
            if conversation_scope_end_position(_validate_reference(end, sources), item,
                    source_message_id=end["source_message_id"], start_utf8=end["start_utf8"]) is None:
                raise ic.ContractViolation("대화 선호의 명시적 종료 근거·순서가 바뀌었다")
    return items


async def published_text(
    session: AsyncSession, *, user_id: uuid.UUID, locale: str,
    retention_enabled: bool | None = None, generation: int = 0,
    activity_date: date | None = None,
    current_text: str | None = None,
) -> str:  # noqa: C901
    """프롬프트에 넣을 계약 블록. 없으면 빈 문자열이다.

    저장된 `rendered_text`를 그대로 쓰지 않고 **정본에서 다시 렌더한다** — 저장분이 옛
    template으로 만들어졌을 수 있고, 렌더 규칙(인용 슬롯)이 바뀌면 옛 문자열은 그 방어를
    받지 못한다.
    """
    enabled = settings.context_retention_v2_enabled if retention_enabled is None else retention_enabled
    if enabled:
        items = await retained_document(session, user_id=user_id, generation=generation)
        active = active_retained_items(items, activity_date=activity_date)
        conversation = [item for item in active if item.get("scope") == "conversation"]
        if conversation:
            active = [item for item in active if not conversation_scope_end_applies(current_text, item)]
            conversation = [item for item in active if item.get("scope") == "conversation"]
            if conversation:
                ends, after = [], 0
                while True:
                    pending = (await session.execute(_PENDING_CONVERSATION_ENDS, {
                        "user_id": user_id, "generation": generation, "after": after,
                    })).all()
                    ends.extend((int(mid), conversation_scope_end_targets(body)) for mid, body in pending
                                if conversation_scope_end_candidate(body))
                    if len(pending) < 200 or all(
                        any(mid >= item["scope_start_message_id"] and ("*" in targets or item["kind"] in targets)
                            for mid, targets in ends)
                        for item in conversation
                    ):
                        break
                    next_after = int(pending[-1][0])
                    if next_after <= after:
                        raise ic.ContractViolation("대화 선호 종료 조회가 진행되지 않는다")
                    after = next_after
                active = [item for item in active if item.get("scope") != "conversation"
                          or not any(mid >= item["scope_start_message_id"]
                              and ("*" in targets or item["kind"] in targets) for mid, targets in ends)]
        directives = [directive_from_item(item) for item in active]
        return ic.render_document(directives, language=locale) if directives else ""
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
            if it.get("status", "active") != "active":
                continue
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
