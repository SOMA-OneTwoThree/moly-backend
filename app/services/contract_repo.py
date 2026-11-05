"""published interaction contract 조회·발행 (6.3절).

계약은 **검색 성공 여부와 무관하게 항상 지켜야 하는 합의**다. 그래서 기억과 달리 회상
경로를 타지 않고 매 턴 프롬프트에 들어간다. 대신 **자주 바뀌지 않으므로** 안정 프리픽스에
둔다 — 실제 조립은 chat.py의 안정 프리픽스에 포함한다.

정본은 locale-neutral `document_json`이고 `rendered_text`는 그 언어별 투영이다. 정본이
같으면(document_hash 동일) 새 version을 만들지 않는다.
"""
from __future__ import annotations

from app.services import message_content

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

from app.services import retention_release, context_compat
from app.services import contract_compiler as compiler


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
SELECT id,content,content_ciphertext,content_chars FROM messages
WHERE user_id=:user_id AND id=ANY(CAST(:ids AS bigint[]))
  AND sender='user' AND kind='normal'
""")

_PENDING_CONVERSATION_ENDS = text("""
SELECT m.id,m.content,m.content_ciphertext,m.content_chars FROM messages m JOIN chat_contexts cc ON cc.user_id=m.user_id
WHERE m.user_id=:user_id AND cc.memory_generation=:generation
  AND m.sender='user' AND m.kind='normal'
  AND m.id>cc.contract_processed_through_message_id
  AND m.id>:after
ORDER BY m.id LIMIT 200
""")


def _source_pairs(rows, user_id):
    return [(row[0], message_content.read_message_content(user_id=user_id, legacy_text=row[1],
        ciphertext=row[2] if len(row)>2 else None, chars=row[3] if len(row)>3 else None)) for row in rows]


def active_retained_items(items: list[dict], *, activity_date: date | None = None) -> list[dict]:
    return [item for item in items if item.get("status", "active") == "active" and not item.get("_compat_native_end")
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


COMPAT_SCHEMA = "retention-contract-compat-v1"


def _legacy_key(item):
    directive = directive_from_item(item)
    return "legacy:" + context_compat.digest({"kind": directive.kind.value,
        "action": directive.action.value, "condition": directive.condition.value,
        "polarity": directive.polarity.value, "target_tag": directive.target_tag,
        "target_literal": directive.target_literal})


def legacy_items(document, baseline, *, check_hash=True, contract_id=None) -> list[dict]:
    """Read adapter only: no invented generation, source hash or processed proof."""
    if check_hash and context_compat.digest(document) != baseline.legacy_contract_hash:
        raise ic.ContractViolation("기존 계약 문서가 바뀌었다")
    result = {}
    try:
        originals = decode_document(document)
    except ic.ContractViolation:
        return []  # Match the original closed-schema reader for malformed legacy JSON.
    for original in originals:
        if original.get("status", "active") != "active":
            continue
        try:
            directive = directive_from_item(original)
        except ic.ContractViolation:
            continue
        item = {"kind": directive.kind.value, "action": directive.action.value,
            "condition": directive.condition.value, "polarity": directive.polarity.value,
            "target_tag": directive.target_tag, "target_literal": directive.target_literal,
            "source_message_id": original.get("source_message_id")}
        item.update(item_key=_legacy_key(item), scope="persistent", status="active",
                    _compat_legacy=baseline.fingerprint,
                    _compat_document_id=str(contract_id or baseline.legacy_contract_id))
        result[item["item_key"]] = item
    return list(result.values())


async def _legacy_items(session, baseline):
    # Late legacy results remain their original format; only known baseline or
    # previously observed legacy heads can be read after superseding.
    rows = (await session.execute(text("""SELECT c.document_json,c.id FROM user_interaction_contracts c
      JOIN chat_contexts cc ON cc.user_id=c.user_id
      WHERE c.user_id=:user_id AND cc.memory_generation=:generation
      AND c.validation_json IS NULL AND c.status IN ('published','superseded')
      AND c.locale=COALESCE((SELECT locale FROM user_interaction_contracts WHERE id=:id),
        (SELECT CASE WHEN lower(language) LIKE 'en%' THEN 'en'
                     WHEN lower(language) LIKE 'ja%' THEN 'ja' ELSE 'ko' END
         FROM profiles WHERE id=:user_id))
      AND (c.status='published' OR c.id=:id OR c.id=ANY(ARRAY(
        SELECT CAST(value AS uuid) FROM user_interaction_contracts d
        CROSS JOIN LATERAL jsonb_array_elements_text(COALESCE(
          d.validation_json->'legacy_contract_ids',
          jsonb_build_array(d.validation_json->>'legacy_contract_id'))) observed(value)
        WHERE d.user_id=:user_id
          AND d.validation_json->'baseline_binding' = CAST(:binding AS jsonb)
          AND d.status IN ('published','superseded'))))
      ORDER BY c.version,c.id"""), {
        "id": uuid.UUID(baseline.legacy_contract_id) if baseline.legacy_contract_id else None,
        "user_id": uuid.UUID(baseline.user_id), "generation": baseline.generation,
        "binding": json.dumps(baseline.payload())})).all()
    if baseline.legacy_contract_id is not None and not any(str(row[1]) == baseline.legacy_contract_id for row in rows):
        raise ic.ContractViolation("기존 계약 문서가 삭제되었다")
    # An incomplete late extraction cannot erase previously visible agreements.
    # Removal/replacement is represented only by a future normal-source override.
    combined = {}
    for document, identity in rows:
        for item in legacy_items(document, baseline,
                check_hash=str(identity) == baseline.legacy_contract_id, contract_id=identity):
            combined[item["item_key"]] = item
    return list(combined.values())


def legacy_end_applies(body, item) -> bool:
    # A conversation closing phrase cannot revoke a persistent legacy preference.
    targets = compiler.conversation_scope_end_targets(body)
    return item.get("kind") in targets


def apply_legacy_ends(items, messages):
    result = [dict(item) for item in items]
    for message in messages:
        if message.sender != "user":
            continue
        for clause, targets, position in compiler._scope_end_clauses(message.content):
            for item in result:
                if item.get("scope", "persistent") != "persistent" or item["kind"] not in targets:
                    continue
                origin = item.get("source_message_id", 0)
                if type(origin) is int and message.id < origin:
                    continue
                start = getattr(message, "start_utf8", 0) + len(message.content[:position].encode())
                if type(origin) is int and message.id == origin and (
                        item.get("_compat_legacy") or type(item.get("end_utf8")) is not int
                        or start < item["end_utf8"]):
                    continue
                reference = {"source_message_id": message.id,
                    "source_hash": getattr(message, "source_hash", None) or hashlib.sha256(message.content.encode()).hexdigest(),
                    "start_utf8": start, "end_utf8": start + len(clause.encode())}
                if item.get("_compat_legacy"):
                    item["status"] = "ended"
                    item["_compat_projection"] = {"op": "end", "legacy_item": {
                        key: value for key, value in item.items() if key not in {"status", "_compat_projection"}},
                        "reference": reference}
                else:
                    item["_compat_native_end"] = reference
    return result


def compatibility_metadata(items, baseline, *, after, through, source_rows):
    native = [item for item in items if not item.get("_compat_legacy")]
    return native, {"schema": COMPAT_SCHEMA, "baseline_binding": baseline.payload(),
        "baseline_fingerprint": baseline.fingerprint, "generation": baseline.generation,
        "legacy_contract_id": next((item.get("_compat_document_id") for item in items if item.get("_compat_legacy") and item.get("status") == "active"), next((item.get("_compat_document_id") for item in items if item.get("_compat_legacy")), None)),
        "legacy_contract_ids": sorted({item["_compat_document_id"] for item in items if item.get("_compat_legacy")}),
        "after": after, "through": through,
        "source_ids": [int(row[0]) for row in source_rows],
        "source_hash": context_compat.digest([list(row) for row in source_rows]),
        "document_hash": context_compat.digest(native),
        "legacy_overrides": {item["item_key"]: item["_compat_projection"] for item in items
                             if item.get("_compat_projection")}}


async def effective_contract_cursor(session, *, user_id, generation, compat_baseline=None):
    if compat_baseline is not None and (compat_baseline.user_id != str(user_id) or compat_baseline.generation != generation):
        raise ic.ContractViolation("기존 계약 연결의 사용자·세대가 바뀌었다")
    if retention_release.binding() is not None:
        actual = await retention_release.contract_cursor(session, user_id=user_id, generation=generation)
    else:
        row = (await session.execute(text("""SELECT contract_generation,
          contract_processed_through_message_id FROM chat_contexts
          WHERE user_id=:user_id AND memory_generation=:generation"""),
          {"user_id": user_id, "generation": generation})).first()
        actual = int(row[1] or 0) if row is not None and row[0] == generation else 0
    return max(actual, compat_baseline.source_after, compat_baseline.legacy_contract_after) if compat_baseline else actual


async def _compat_document(session, *, user_id, generation, baseline):
    if baseline.user_id != str(user_id) or baseline.generation != generation:
        raise ic.ContractViolation("기존 계약 연결의 사용자·세대가 바뀌었다")
    legacy = await _legacy_items(session, baseline)
    legacy_snapshot = list(legacy)
    row = (await session.execute(text("""SELECT id,document_json,validation_json
      FROM user_interaction_contracts WHERE user_id=:user_id
      AND status IN ('published','superseded') AND validation_json IS NOT NULL
      AND version>COALESCE((SELECT version FROM user_interaction_contracts WHERE id=:baseline_id),0)
      ORDER BY version DESC,id DESC LIMIT 1"""), {"user_id": user_id, "baseline_id": uuid.UUID(baseline.legacy_contract_id) if baseline.legacy_contract_id else None})).first()
    if row is None:
        return legacy
    metadata = row[2]
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError as exc:
            raise ic.ContractViolation("신규 계약 연결 JSON이 잘못됐다") from exc
    native = decode_document(row[1])
    if (not isinstance(metadata, dict) or metadata.get("schema") != COMPAT_SCHEMA
            or metadata.get("baseline_binding") != baseline.payload()
            or metadata.get("baseline_fingerprint") != baseline.fingerprint
            or type(metadata.get("generation")) is not int or metadata.get("generation") != generation
            or metadata.get("document_hash") != context_compat.digest(native)):
        raise ic.ContractViolation("기존 계약과 신규 변경의 연결이 잘못됐다")
    floor = max(baseline.source_after, baseline.legacy_contract_after)
    if (type(metadata.get("after")) is not int or type(metadata.get("through")) is not int
            or not floor <= metadata["after"] <= metadata["through"]
            or any(type(item.get("source_message_id")) is not int or item["source_message_id"] <= floor for item in native)):
        raise ic.ContractViolation("신규 계약의 처리 범위가 잘못됐다")
    overrides = metadata.get("legacy_overrides")
    if not isinstance(overrides, dict):
        raise ic.ContractViolation("기존 계약 변경 대상이 잘못됐다")
    keys = {item["item_key"] for item in legacy}
    for key, projection in overrides.items():
        if key not in keys:
            snapshot = projection.get("legacy_item") if isinstance(projection, dict) else None
            if not isinstance(snapshot, dict) or _legacy_key(snapshot) != key:
                raise ic.ContractViolation("기존 계약 변경 대상의 보존 기록이 없다")
            legacy.append({**snapshot, "status": "active"})
    for item in legacy:
        if item["item_key"] in overrides:
            item["status"] = "ended"
            item["_compat_projection"] = overrides[item["item_key"]]
    return await validate_retained_items(session, user_id=user_id, generation=generation,
        items=[*legacy, *native], compat_baseline=baseline, _legacy_snapshot=legacy_snapshot)


async def retained_document(session: AsyncSession, *, user_id: uuid.UUID,
                            generation: int, compat_baseline=None) -> list[dict]:
    """현 세대의 원문 검증본만 반환. 손상/삭제/재분류를 빈 선호로 숨기지 않는다."""
    if compat_baseline is not None:
        return await _compat_document(session, user_id=user_id, generation=generation, baseline=compat_baseline)
    if retention_release.binding() is not None:
        row = await retention_release.contract_row(session, user_id=user_id, generation=generation)
    else:
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
                                  generation: int, items: list[dict], compat_baseline=None, _legacy_snapshot=None) -> list[dict]:
    """분할 작업의 미게시 draft도 원본 ID·구간·전체 지문으로 검증한다."""
    if not items:
        return []
    if compat_baseline is not None:
        original_items = await _legacy_items(session, compat_baseline) if _legacy_snapshot is None else _legacy_snapshot
        originals = {item["item_key"]: item for item in original_items}
        legacy = [item for item in items if item.get("_compat_legacy")]
        native = [item for item in items if not item.get("_compat_legacy")]
        if len({item.get("item_key") for item in legacy}) != len(legacy) or set(originals) - {item.get("item_key") for item in legacy}:
            raise ic.ContractViolation("기존 계약 항목이 누락되거나 위조됐다")
        native = await validate_retained_items(session, user_id=user_id, generation=generation, items=native)
        refs = []
        for item in legacy:
            original = originals.get(item["item_key"])
            if (_legacy_key(item) != item["item_key"] or item.get("_compat_legacy") != compat_baseline.fingerprint
                    or (original is None and item.get("status") != "ended")
                    or item.get("status") not in {"active", "ended"}):
                raise ic.ContractViolation("기존 계약 항목의 내용이 바뀌었다")
            projection = item.get("_compat_projection")
            if item["status"] == "active":
                if projection is not None:
                    raise ic.ContractViolation("적용 중인 기존 계약의 종료 근거가 섞였다")
                continue
            if not isinstance(projection, dict) or projection.get("op") not in {"end", "replace"} or not isinstance(projection.get("reference"), dict):
                raise ic.ContractViolation("기존 계약 변경 근거가 없다")
            ref = projection["reference"]
            if type(ref.get("source_message_id")) is not int or ref["source_message_id"] <= max(compat_baseline.source_after, compat_baseline.legacy_contract_after):
                raise ic.ContractViolation("기존 계약 변경이 신규 원문 범위 밖이다")
            refs.append((item, projection, ref))
        sources = {row[0]: message_content.read_message_content(user_id=user_id, legacy_text=row[1],
            ciphertext=row[2] if len(row)>2 else None, chars=row[3] if len(row)>3 else None) for row in (await session.execute(_VALID_SOURCES,
            {"user_id": user_id, "ids": [ref["source_message_id"] for _, _, ref in refs]})).all()} if refs else {}
        for item, projection, ref in refs:
            quote = _validate_reference(ref, sources)
            if projection["op"] == "end" and not legacy_end_applies(quote, item):
                raise ic.ContractViolation("기존 계약의 명시적 종료 근거가 바뀌었다")
            if projection["op"] == "replace":
                replacement = projection.get("replacement")
                if isinstance(replacement, dict):
                    await validate_retained_items(session, user_id=user_id, generation=generation, items=[replacement])
                if not isinstance(replacement, dict) or any(replacement.get(key) != ref.get(key) for key in ("source_message_id", "source_hash", "start_utf8", "end_utf8")) or replacement["kind"] != item["kind"]:
                    raise ic.ContractViolation("기존 계약 정정의 새 근거가 없다")
        return [*legacy, *native]
    if any(type(item.get("generation")) is not int or item["generation"] != generation for item in items):
        raise ic.ContractViolation("계약 draft의 기억 세대가 바뀌었다")
    items = [_scope_metadata(item) for item in items]
    if len({item["item_key"] for item in items}) != len(items):
        raise ic.ContractViolation("계약 항목 키가 중복됐다")
    ids = [item.get("source_message_id") for item in items]
    ids.extend(item["scope_end"]["source_message_id"] for item in items if item.get("status") == "ended")
    for item in items:
        end = item.get("_compat_native_end")
        if end is not None:
            if not isinstance(end, dict) or set(end) != {"source_message_id", "source_hash", "start_utf8", "end_utf8"}:
                raise ic.ContractViolation("신규 영구 선호 종료의 근거가 잘못됐다")
            ids.append(end["source_message_id"])
    if any(type(value) is not int or value <= 0 for value in ids):
        raise ic.ContractViolation("계약 근거 메시지 ID가 잘못됐다")
    sources = {row[0]: message_content.read_message_content(user_id=user_id, legacy_text=row[1],
            ciphertext=row[2] if len(row)>2 else None, chars=row[3] if len(row)>3 else None) for row in (await session.execute(
        _VALID_SOURCES, {"user_id": user_id, "ids": ids},
    )).all()}
    for item in items:
        directive_from_item(item)
        _validate_reference(item, sources)
        if item.get("_compat_native_end") is not None:
            end = item["_compat_native_end"]
            if (item["scope"] != "persistent" or end["source_message_id"] < item["source_message_id"]
                    or (end["source_message_id"] == item["source_message_id"] and end["start_utf8"] < item["end_utf8"])
                    or not legacy_end_applies(_validate_reference(end, sources), item)):
                raise ic.ContractViolation("신규 영구 선호의 명시적 종료 근거가 바뀌었다")
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
    compat_baseline=None,
) -> str:  # noqa: C901
    """프롬프트에 넣을 계약 블록. 없으면 빈 문자열이다.

    저장된 `rendered_text`를 그대로 쓰지 않고 **정본에서 다시 렌더한다** — 저장분이 옛
    template으로 만들어졌을 수 있고, 렌더 규칙(인용 슬롯)이 바뀌면 옛 문자열은 그 방어를
    받지 못한다.
    """
    enabled = settings.context_retention_v2_enabled if retention_enabled is None else retention_enabled
    if enabled or compat_baseline is not None:
        items = await retained_document(session, user_id=user_id, generation=generation, compat_baseline=compat_baseline)
        end_sql, end_params = _PENDING_CONVERSATION_ENDS, {}
        if retention_release.binding() is not None:
            end_params["contract_through"] = await retention_release.contract_cursor(
                session, user_id=user_id, generation=generation)
            end_sql = text(str(end_sql).replace("m.id>cc.contract_processed_through_message_id",
                                               "m.id>:contract_through"))
        active = active_retained_items(items, activity_date=activity_date)
        if compat_baseline is not None:
            active = [item for item in active if not (item.get("scope", "persistent") == "persistent" and legacy_end_applies(current_text, item))]
            # Stored, still-uncompiled explicit changes must also take effect now.
            after = max(compat_baseline.source_after, compat_baseline.legacy_contract_after)
            while any(item.get("scope", "persistent") == "persistent" for item in active):
                pending = (await session.execute(_PENDING_CONVERSATION_ENDS, {"user_id": user_id,
                    "generation": generation, "after": after})).all()
                pending = _source_pairs(pending, user_id)
                active = [item for item in active if not (item.get("scope", "persistent") == "persistent" and any(
                    mid > (item.get("source_message_id") or 0) and legacy_end_applies(body, item) for mid, body in pending))]
                if len(pending) < 200:
                    break
                if pending[-1][0] <= after:
                    raise ic.ContractViolation("기존 계약 종료 조회가 진행되지 않는다")
                after = pending[-1][0]
        conversation = [item for item in active if item.get("scope") == "conversation"]
        if conversation:
            active = [item for item in active if not conversation_scope_end_applies(current_text, item)]
            conversation = [item for item in active if item.get("scope") == "conversation"]
            if conversation:
                ends, after = [], 0
                while True:
                    pending = (await session.execute(end_sql, {**end_params,
                        "user_id": user_id, "generation": generation, "after": after,
                    })).all()
                    pending = _source_pairs(pending, user_id)
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
    if retention_release.binding() is not None:
        return False  # Bound writes also certify a new consumed-source boundary.
    n = await session.scalar(_SAME_DOCUMENT, {"user_id": user_id, "hash": document_hash})
    return bool(n)
