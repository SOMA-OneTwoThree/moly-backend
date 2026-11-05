"""interaction contract 추출·발행 잡 (6.3절).

**캐피의 단일 추측은 published contract를 직접 바꾸지 못한다.** 그래서 이 잡은 후보를
만들되, 영향이 큰 항목은 draft로 남기고 낮은 것만 publish한다.

영향이 큰 것 = 경계(피할 주제·표현)와 관계 정의. 이건 잘못 걸면 캐피가 사용자가 하지 않은
약속 때문에 말을 못 하게 된다. 나머지(호칭·말투·위로 방식)는 6.3절대로 되묻지 않고 바로
publish한다 — 매번 "이렇게 해줄까?"로 확인하면 페르소나의 질문 절약 규칙과 충돌하고,
사용자가 확답 없이 화제를 넘기면 합의가 또 사라진다.
"""
from __future__ import annotations

from app.services import message_content

import json
import logging
import hashlib
from dataclasses import dataclass

from sqlalchemy import text

from app.config import settings
from app.core.db import get_sessionmaker
from app.services import contract_compiler as cc
from app.services import contract_repo, retention_release, context_compat
from app.services import interaction_contract as ic
from app.services import llm, usage_ledger, context_retention, checkpoint
from app.services.jobs import ClaimedJob
from worker import consumer
from worker.consumer import JobCancelled, JobFatal, JobResult, JobRetry
from app.services import i18n

_log = logging.getLogger("moly-worker")

JOB_CONTRACT_COMPILE = "contract_compile"

_MAX_SOURCE = 200
_MAX_OUTPUT_TOKENS = 2_000

# 사용자 확인 전에는 publish하지 않는 영역(6.3절). 잘못 걸면 캐피가 하지 않은 약속 때문에
# 말을 못 하게 되므로, 이쪽만 draft로 남긴다.
HIGH_IMPACT = frozenset({
    ic.Kind.TOPIC_BOUNDARY,
    ic.Kind.EXPRESSION_BOUNDARY,
    ic.Kind.RELATIONSHIP_DEFINITION,
    ic.Kind.CUSTOM_PREFERENCE,
})

# ⚠️ **최근 것부터** 가져와 다시 시간순으로 세운다.
#
# 예전엔 `ORDER BY id LIMIT 200`이라 **가장 오래된 200건**을 봤다. 대화가 200건을 넘는
# 순간부터 새 발화는 영원히 안 읽혀서, 사용자가 "앞으로 반말로 해줘"라고 해도 계약이
# 만들어지지 않았다(실측: 240건 중 #1~#207만 봄, 요청은 #244).
_MESSAGES = text("""
SELECT id, sender, content, content_ciphertext, content_chars FROM (
  SELECT id, sender, content, content_ciphertext, content_chars FROM messages
  WHERE user_id = :user_id AND kind = 'normal' AND id > :after
  ORDER BY id DESC LIMIT :limit
) recent
ORDER BY id
""")

_LEGACY_SOURCE = text("""
SELECT id,sender,content,content_ciphertext,content_chars FROM messages
WHERE user_id=:user_id AND kind='normal' AND id=ANY(CAST(:ids AS bigint[]))
ORDER BY id
""")

_PROFILE = text("SELECT nickname, language FROM profiles WHERE id = :user_id")

_NEXT_VERSION = text("""
SELECT COALESCE(max(version), 0) + 1 FROM user_interaction_contracts
WHERE user_id = :user_id AND locale = :locale
""")

_INSERT = text("""
INSERT INTO user_interaction_contracts
  (user_id, version, locale, document_json, rendered_text, render_hash, status)
VALUES (:user_id, :version, :locale, CAST(:doc AS jsonb), :rendered, :hash, 'draft')
RETURNING id
""")

_INSERT_ITEM = text("""
INSERT INTO user_interaction_contract_items
  (contract_id, user_id, item_key, section, value_json, rendered_text,
   authority, source_message_id, status)
VALUES (:contract_id, :user_id, :item_key, :section, CAST(:value AS jsonb), :rendered,
        'explicit_user', :source_message_id, :item_status)
ON CONFLICT (contract_id, item_key) DO NOTHING
""")

_SECTION = {
    ic.Kind.ADDRESS: "address_policy",
    ic.Kind.RESPONSE_STYLE: "communication_style",
    ic.Kind.COMFORT: "comfort_style",
    ic.Kind.TOPIC_BOUNDARY: "boundaries",
    ic.Kind.EXPRESSION_BOUNDARY: "boundaries",
    ic.Kind.RELATIONSHIP_DEFINITION: "relationship_frame",
    ic.Kind.DURABLE_BEHAVIOR: "durable_commitments",
    ic.Kind.CUSTOM_PREFERENCE: "durable_commitments",
}


def _item_key(d: ic.Directive) -> str:
    target = d.target_literal or d.target_tag or ""
    return f"{d.kind.value}:{d.action.value}:{d.condition.value}:{target}"


def _document(candidates) -> str:
    """locale-neutral 정본. 렌더 문자열이 아니라 이게 hash 대상이다."""
    return json.dumps([{
        "kind": c.directive.kind.value,
        "action": c.directive.action.value,
        "condition": c.directive.condition.value,
        "polarity": c.directive.polarity.value,
        "target_tag": c.directive.target_tag,
        "target_literal": c.directive.target_literal,
        "source_message_id": c.source_message_id,
    } for c in candidates], ensure_ascii=False, sort_keys=True)


def _logical_source_rows(rows, user_id, width):
    return [(*row[:2], message_content.read_message_content(user_id=user_id, legacy_text=row[2],
        ciphertext=row[width] if len(row)>width else None,
        chars=row[width+1] if len(row)>width+1 else None), *row[3:width]) for row in rows]


async def handle_contract_compile(job: ClaimedJob) -> JobResult:
    if retention_release.binding() is not None and (job.payload or {}).get("retention_enabled") is not True:
        raise JobFatal("preparation_requires_retention_contract")
    if (job.payload or {}).get("retention_enabled") is True:
        return await _handle_retention_compile(job)
    if job.user_id is None:
        raise JobFatal("missing_user")
    uid = job.user_id
    payload = job.payload or {}
    after = int(payload.get("after_message_id", 0))
    source_ids = payload.get("source_ids")
    if source_ids is not None and (not isinstance(source_ids, list) or not source_ids
            or len(source_ids) > _MAX_SOURCE
            or any(type(mid) is not int or mid <= 0 for mid in source_ids)
            or source_ids != sorted(set(source_ids))):
        raise JobFatal("invalid_contract_source_ids")

    async with get_sessionmaker()() as session:
        if await context_retention.adopted_user(session, user_id=uid):
            raise JobCancelled("retention_user_legacy_contract")
        rows = (await session.execute(_LEGACY_SOURCE if source_ids else _MESSAGES,
            {"user_id": uid, "ids": source_ids} if source_ids else
            {"user_id": uid, "after": after, "limit": _MAX_SOURCE})).all()
        profile = (await session.execute(_PROFILE, {"user_id": uid})).first()
    if not rows:
        raise JobCancelled("no_source_messages")

    # 저장·조회 모두 콘텐츠 언어 버킷(ko·en·ja)으로 맞춘다. 예전에는 원본 태그를 그대로 써서
    # "ko-KR"과 "ko"가 서로 다른 계약으로 갈렸고, 관계 렌더(i18n.resolve 사용)와도 어긋났다.
    locale = i18n.resolve(profile[1] if profile else None)
    rows = _logical_source_rows(rows, uid, 3)
    msgs = [_Msg(r[0], r[1], r[2]) for r in rows]
    user_ids = {m.id for m in msgs if m.sender == "user"}
    manifest = _contract_source_manifest(msgs, locale=locale)
    if source_ids and ([m.id for m in msgs] != source_ids or payload.get("source_manifest") != manifest):
        raise JobCancelled("contract_source_changed")
    try:
        batch = contract_batch(msgs, previous=[], system=cc.SYSTEM,
            start_index=payload.get("source_index", 0), start_offset=payload.get("source_offset", 0))
        carried, invalid = cc.parse(json.dumps(payload.get("candidates", [])), user_message_ids=user_ids)
        if invalid:
            raise cc.CompilerRejection("잘못된 선호 분할 중간 결과")
    except cc.CompilerRejection as exc:
        raise JobRetry("contract_background_budget") from exc
    model_input = _model_input(batch.messages, [])
    context_retention.assert_input_budget(cc.SYSTEM, model_input, output_reserve=_MAX_OUTPUT_TOKENS)

    try:
        result = await llm.generate(
            cc.SYSTEM,
            model_input,
            model=settings.model_utility, reasoning_effort="none", max_tokens=_MAX_OUTPUT_TOKENS,
            ledger=usage_ledger.LedgerContext(
                lane=usage_ledger.LANE_BACKGROUND, purpose="contract_compile",
                user_id=uid, job_id=job.id, attempt=job.attempt,
            ),
        )
    except Exception as e:  # noqa: BLE001  provider 장애 — 재시도
        raise JobRetry("contract_llm_failed") from e

    try:
        llm.require_completed_text(result.text, result.finish_reason)
        kept, dropped = cc.parse(result.text,
            user_message_ids={m.id for m in batch.messages if m.sender == "user"})
    except cc.CompilerRejection as e:
        raise JobRetry("contract_schema") from e
    if dropped:
        _log.info("계약 후보 폐기 %d건 — job=%s", len(dropped), job.id)
    kept = [*carried, *kept]
    if batch.next_index < len(msgs):
        async def _continue(session) -> None:
            if await context_retention.adopted_user(session, user_id=uid, lock=True):
                return
            current = (await session.execute(_LEGACY_SOURCE,
                {"user_id": uid, "ids": [m.id for m in msgs]})).all()
            current = _logical_source_rows(current, uid, 3)
            current_msgs = [_Msg(r[0], r[1], r[2]) for r in current]
            if _contract_source_manifest(current_msgs, locale=locale) != manifest:
                return
            from app.services import jobs
            await jobs.enqueue(session, queue=jobs.QUEUE_CONTENT, job_type=JOB_CONTRACT_COMPILE,
                user_id=uid,
                dedup_key=f"contract-parts:{uid}:{manifest}:{batch.next_index}:{batch.next_offset}",
                payload={**payload, "source_ids": [m.id for m in msgs], "source_manifest": manifest,
                    "source_index": batch.next_index, "source_offset": batch.next_offset,
                    "candidates": json.loads(_document(kept))})
        return JobResult(result_code="continued", apply_domain=_continue)
    if not kept:
        return JobResult(result_code="no_candidates")

    doc = _document(kept)
    digest = ic.document_hash(doc)
    # ⚠️ language를 안 넘기면 기본값 ko라서 영어·일본어 유저의 rendered_text가 한국어로 저장된다.
    rendered = ic.render_document([c.directive for c in kept], language=locale)
    auto_publish = not any(c.directive.kind in HIGH_IMPACT for c in kept)

    async def _apply(session) -> None:
        if await context_retention.adopted_user(session, user_id=uid, lock=True):
            return
        current = (await session.execute(_LEGACY_SOURCE,
            {"user_id": uid, "ids": [m.id for m in msgs]})).all()
        current = _logical_source_rows(current, uid, 3)
        if _contract_source_manifest([_Msg(r[0], r[1], r[2]) for r in current], locale=locale) != manifest:
            return
        # 정본이 같으면 새 version을 만들지 않는다(6.3절).
        if await contract_repo.has_same_document(session, user_id=uid, document_hash=digest):
            return
        version = int(await session.scalar(_NEXT_VERSION, {"user_id": uid, "locale": locale}))
        cid = await session.scalar(_INSERT, {
            "user_id": uid, "version": version, "locale": locale,
            "doc": doc, "rendered": rendered, "hash": digest,
        })
        for c in kept:
            await session.execute(_INSERT_ITEM, {
                "contract_id": cid, "user_id": uid,
                "item_key": _item_key(c.directive),
                "section": _SECTION[c.directive.kind],
                "value": json.dumps({
                    "action": c.directive.action.value,
                    "condition": c.directive.condition.value,
                    "polarity": c.directive.polarity.value,
                    "target_tag": c.directive.target_tag,
                    "target_literal": c.directive.target_literal,
                }, ensure_ascii=False),
                "rendered": ic.render(c.directive, language=locale),
                "source_message_id": c.source_message_id,
                "item_status": "active",
            })
        if auto_publish:
            await contract_repo.publish(
                session, contract_id=cid, user_id=uid, locale=locale
            )

    return JobResult(
        result_code="ok",
        result_detail={"kept": len(kept), "dropped": len(dropped),
                       "published": auto_publish},
        apply_domain=_apply,
    )


class _Msg:
    __slots__ = ("id", "sender", "content", "activity_date", "start_utf8", "end_utf8", "source_hash")

    def __init__(self, id: int, sender: str, content: str, activity_date=None,
                 start_utf8=0, end_utf8=None, source_hash=None):
        self.id, self.sender, self.content = id, sender, content
        self.activity_date = activity_date
        self.start_utf8, self.end_utf8, self.source_hash = start_utf8, end_utf8, source_hash


def _contract_source_manifest(messages, *, locale: str) -> str:
    value = {"locale": locale, "model": settings.model_utility,
             "messages": [{"id": m.id, "sender": m.sender,
                 "hash": hashlib.sha256(m.content.encode()).hexdigest()} for m in messages]}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ContractBatch:
    messages: tuple[_Msg, ...]
    next_index: int
    next_offset: int


def _prompt_previous(items: list[dict], *, activity_date=None) -> list[dict]:
    """기존 본문·근거는 서버가 보존한다. 모델에는 수정 대상과 짧은 적용 내용만 준다."""
    return [{"item_key": item["item_key"], "scope": item.get("scope", "persistent"),
             "text": ic.render(contract_repo.directive_from_item(item), language="en")}
            for item in contract_repo.active_retained_items(items, activity_date=activity_date)]


def _model_input(messages, previous: list[dict]) -> list[dict]:
    activity_date = messages[-1].activity_date if messages else None
    return [{"role": "user", "content": cc.render_conversation(messages)
             + "\n[기존 선호 데이터]\n" + json.dumps(
                 _prompt_previous(previous, activity_date=activity_date), ensure_ascii=False)}]


def contract_batch(messages, *, previous: list[dict], system: str,
                   start_index: int = 0, start_offset: int = 0,
                   retention_enabled: bool = False) -> ContractBatch:
    """행 수는 조회 상한일 뿐이다. 전체 배경 입력 예산에 맞는 연속 원문만 선택한다."""
    if (type(start_index) is not int or not 0 <= start_index < len(messages)
            or type(start_offset) is not int or start_offset < 0):
        raise cc.CompilerRejection("잘못된 선호 분할 위치")
    if retention_enabled:
        return _retention_contract_batch(messages, previous=previous, system=system,
            start_index=start_index, start_offset=start_offset)
    chosen = []
    for index in range(start_index, len(messages)):
        message = messages[index]
        body = message.content.encode("utf-8")
        offset = start_offset if index == start_index else 0
        if offset > len(body):
            raise cc.CompilerRejection("선호 분할 위치가 원문 밖이다")
        try:
            remaining = body[offset:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise cc.CompilerRejection("선호 분할 위치가 UTF-8 경계를 자른다") from exc
        whole_hash = hashlib.sha256(body).hexdigest()
        source = checkpoint.SourceMessage(message.id, message.sender, "normal", remaining,
            start_utf8=offset, end_utf8=len(body), source_hash=whole_hash)
        if offset or len(body) > checkpoint.BACKGROUND_SEGMENT_BYTES:
            if chosen:
                return ContractBatch(tuple(chosen), index, 0)
            size = checkpoint.BACKGROUND_SEGMENT_BYTES
            while size >= 4:
                piece = checkpoint.source_segments(source, max_bytes=size)[0]
                part = _Msg(message.id, message.sender, piece.content, message.activity_date,
                            piece.start_utf8, piece.end_utf8, piece.source_hash)
                try:
                    context_retention.assert_input_budget(system, _model_input([part], previous),
                        output_reserve=_MAX_OUTPUT_TOKENS)
                except context_retention.RetentionError:
                    size //= 2
                    continue
                complete = piece.end_utf8 == len(body)
                return ContractBatch((part,), index + 1 if complete else index,
                                     0 if complete else piece.end_utf8)
            raise cc.CompilerRejection("선호 배경 입력 예산을 넘었다")
        candidate = [*chosen, message]
        try:
            context_retention.assert_input_budget(system, _model_input(candidate, previous),
                                                  output_reserve=_MAX_OUTPUT_TOKENS)
            fits = sum(len(m.content.encode()) for m in candidate) <= checkpoint.BACKGROUND_SEGMENT_BYTES
        except context_retention.RetentionError:
            fits = False
        if not fits:
            if not chosen:
                raise cc.CompilerRejection("선호 배경 입력 예산을 넘었다")
            return ContractBatch(tuple(chosen), index, 0)
        chosen.append(message)
    return ContractBatch(tuple(chosen), len(messages), 0)


def _retention_contract_batch(messages, *, previous, system, start_index, start_offset):
    """Fill the unchanged serialized budget; only the first source may be partial.

    A later partial source cannot share a batch with earlier complete sources:
    the durable partial cursor deliberately remains at the original first row.
    """
    def fits(selected):
        return context_retention.fits_input_budget(system, _model_input(selected, previous),
            output_reserve=_MAX_OUTPUT_TOKENS)

    chosen = []
    for index in range(start_index, len(messages)):
        message = messages[index]
        body = message.content.encode("utf-8")
        offset = start_offset if index == start_index else 0
        if offset > len(body):
            raise cc.CompilerRejection("선호 분할 위치가 원문 밖이다")
        try:
            remaining = body[offset:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise cc.CompilerRejection("선호 분할 위치가 UTF-8 경계를 자른다") from exc
        if not offset and fits([*chosen, message]):
            chosen.append(message)
            continue
        if chosen:
            return ContractBatch(tuple(chosen), index, 0)

        digest = hashlib.sha256(body).hexdigest()

        def part(characters):
            text = remaining[:characters]
            return _Msg(message.id, message.sender, text, message.activity_date,
                        offset, offset + len(text.encode("utf-8")), digest)

        # Prefix length is measured in Unicode characters, so every tested end
        # is a UTF-8 boundary. Test the real JSON wire, including escaping and
        # the existing contract, rather than estimating room from source bytes.
        low, high, best = 1, len(remaining), 0
        while low <= high:
            middle = (low + high) // 2
            if fits([part(middle)]):
                best, low = middle, middle + 1
            else:
                high = middle - 1
        if not best:
            raise cc.CompilerRejection("선호 배경 입력 예산을 넘었다")
        selected = part(best)
        complete = selected.end_utf8 == len(body)
        return ContractBatch((selected,), index + 1 if complete else index,
                             0 if complete else selected.end_utf8)
    return ContractBatch(tuple(chosen), len(messages), 0)


_RETENTION_STATE = text("""
SELECT memory_generation,contract_generation,contract_processed_through_message_id
FROM chat_contexts WHERE user_id=:user_id
""")
_RETENTION_STATE_LOCK = text(str(_RETENTION_STATE) + " FOR UPDATE")
_RETENTION_MESSAGES = text("""
SELECT id,sender,content,activity_date,content_ciphertext,content_chars FROM messages
WHERE user_id=:user_id AND kind='normal' AND id>:after AND id<=:through
ORDER BY id LIMIT :limit
""")
_RETENTION_ADVANCE = text("""
UPDATE chat_contexts SET contract_generation=:generation,
 contract_processed_through_message_id=:through
WHERE user_id=:user_id AND memory_generation=:generation
""")
_RETENTION_INSERT = text("""
INSERT INTO user_interaction_contracts
 (user_id,version,locale,document_json,rendered_text,render_hash,status,source_watermark,validation_json)
VALUES (:user_id,:version,:locale,CAST(:doc AS jsonb),:rendered,:hash,'draft',:through,CAST(:validation AS jsonb))
RETURNING id
""")


def _retention_item(candidate: cc.Candidate, generation: int) -> dict:
    directive = candidate.directive
    return {
        "kind": directive.kind.value, "action": directive.action.value,
        "condition": directive.condition.value, "polarity": directive.polarity.value,
        "target_tag": directive.target_tag, "target_literal": directive.target_literal,
        "source_message_id": candidate.source_message_id, "scope": candidate.scope,
        "source_hash": candidate.source_hash, "start_utf8": candidate.start_utf8,
        "end_utf8": candidate.end_utf8, "activity_date": candidate.activity_date,
        "generation": generation,
        "item_key": f"{candidate.scope}:{_item_key(directive)}",
        "status": "active",
        "scope_start_message_id": candidate.source_message_id if candidate.scope == "conversation" else None,
        "scope_end": None,
    }


def merge_retained_items(previous: list[dict], candidates: list[cc.Candidate],
                         *, generation: int) -> list[dict]:
    """모델이 빠뜨린 항목은 복사한다. 명시한 같은 범위의 정정만 바꾼다."""
    items = {item["item_key"]: dict(item) for item in previous}
    for candidate in candidates:
        if candidate.operation == "end":
            old = items.get(candidate.ends_item_key)
            if (old is None or old.get("scope") != "conversation"
                    or old.get("status", "active") != "active"):
                raise cc.CompilerRejection("끝낼 적용 중 대화 선호가 없다")
            start = old.get("scope_start_message_id", old.get("source_message_id"))
            if type(start) is not int or candidate.source_message_id < start:
                raise cc.CompilerRejection("대화 선호 종료가 시작보다 앞선다")
            if candidate.source_message_id == start:
                position = candidate.end_clause_start_utf8
                if type(position) is not int or position < old["end_utf8"]:
                    continue  # 같은 원문의 뒤쪽에서 새로 시작한 선호는 앞 종료가 닫지 않는다.
            old["scope_start_message_id"] = start
            old["status"] = "ended"
            old["scope_end"] = {"source_message_id": candidate.source_message_id,
                "source_hash": candidate.source_hash, "start_utf8": candidate.start_utf8,
                "end_utf8": candidate.end_utf8}
            continue
        item = _retention_item(candidate, generation)
        old_key = candidate.supersedes_item_key
        if old_key is not None:
            old = items.get(old_key)
            if old is None or old.get("scope") != candidate.scope or old.get("kind") != item["kind"]:
                raise cc.CompilerRejection("정정할 기존 선호의 범위가 맞지 않는다")
            if old.get("_compat_legacy"):
                old["status"] = "ended"
                old["_compat_projection"] = {"op": "replace", "legacy_item": {key: value for key, value in old.items() if key not in {"status", "_compat_projection"}}, "replacement_key": item["item_key"],
                    "replacement": dict(item), "reference": {key: item[key] for key in
                        ("source_message_id", "source_hash", "start_utf8", "end_utf8")}}
            else:
                del items[old_key]
        # Repeating an identical legacy directive does not create two prompt entries.
        for old in items.values():
            if (old.get("_compat_legacy") and old.get("status") == "active"
                    and contract_repo.directive_from_item(old) == candidate.directive):
                old["status"] = "ended"
                old["_compat_projection"] = {"op": "replace", "legacy_item": {key: value for key, value in old.items() if key not in {"status", "_compat_projection"}}, "replacement_key": item["item_key"],
                    "replacement": dict(item), "reference": {key: item[key] for key in
                        ("source_message_id", "source_hash", "start_utf8", "end_utf8")}}
        items[item["item_key"]] = item
    return [items[key] for key in sorted(items)]


async def _persist_output_fallback(job, before, after):
    """Only control metadata, never truncated output; no schema or shared-core change."""
    wire = json.dumps(after, sort_keys=True, separators=(",", ":"))
    async with get_sessionmaker()() as session:
        if not await context_retention.privacy_epoch_matches(session, user_id=job.user_id,
                privacy_epoch=before.get("privacy_epoch"), lock=True):
            raise JobCancelled("contract_privacy_epoch_changed")
        row = (await session.execute(text("""
          UPDATE async_jobs j SET payload=CAST(:payload AS jsonb),payload_hash=:payload_hash
          WHERE j.id=:job_id AND j.user_id=:user_id AND j.state='running'
            AND j.lease_owner=:owner AND j.lease_token=:token AND j.lease_until>now()
            AND j.payload=CAST(:before AS jsonb) AND j.payload_redacted_at IS NULL
            AND (j.payload_expires_at IS NULL OR j.payload_expires_at>now())
            AND EXISTS(SELECT 1 FROM chat_contexts c WHERE c.user_id=j.user_id
                       AND c.memory_generation=:generation)
          RETURNING j.id
        """), {"payload":wire,"payload_hash":hashlib.sha256(wire.encode()).hexdigest(),
                "before":json.dumps(before,sort_keys=True,separators=(",", ":")),
                "job_id":job.id,"user_id":job.user_id,"owner":job.lease_owner,
                "token":job.lease_token,"generation":before["memory_generation"]})).first()
        if row is None:
            raise JobCancelled("contract_fallback_snapshot_changed")
        await session.commit()


async def _retention_state(session, uid, generation, *, lock=False):
    # cc protects generation/concurrent writers; preparation cursor comes only
    # from that preparation's independently certified document, including [].
    row = (await session.execute(_RETENTION_STATE_LOCK if lock else _RETENTION_STATE,
                                 {"user_id": uid})).first()
    if row is not None and retention_release.binding() is not None:
        cursor = await retention_release.contract_cursor(session, user_id=uid, generation=generation)
        return (row[0], generation, cursor)
    return row


def _effective_cursor(state, generation: int, baseline) -> int:
    """실제로 이어 읽을 커서. 선호 세대가 다르면 0, 호환 기준선이 있으면 그 경계까지 올린다.

    state = `_retention_state` 결과(memory_generation, contract_generation, 처리한 마지막 message id).
    """
    cursor = int(state[2]) if int(state[1]) == generation else 0
    if baseline is not None:
        cursor = max(cursor, baseline.source_after, baseline.legacy_contract_after)
    return cursor


async def _handle_retention_compile(job: ClaimedJob) -> JobResult:
    if job.user_id is None:
        raise JobFatal("missing_user")
    uid = job.user_id
    payload = job.payload or {}
    fallback_planner = payload.get("adaptive_output_fallback", False)
    if type(fallback_planner) is not bool:
        raise JobFatal("invalid_contract_output_fallback")
    fallback_available = payload.get("adaptive_output_attempts_remaining")
    if fallback_planner and (type(fallback_available) is not int or fallback_available < 0):
        raise JobFatal("invalid_contract_output_fallback")
    if fallback_planner and fallback_available == 0:
        raise JobFatal("contract_fallback_attempts_exhausted")
    generation = payload.get("memory_generation")
    privacy_epoch = payload.get("privacy_epoch")
    target = payload.get("through_message_id")
    if type(generation) is not int or generation < 0 or type(target) is not int or target <= 0:
        raise JobFatal("invalid_contract_retention_payload")
    # dead 뒤 다시 만든 작업(context_retention.enqueue_contract_processing)의 표시. 이어지는 작업의
    # dedup_key에 붙여, 죽은 작업이 이미 만들어 둔 같은 범위의 키와 겹쳐 조용히 끊기지 않게 한다.
    redo_of, redo_depth = payload.get("redo_of"), payload.get("redo_depth")
    redo_marker = ({"redo_of": redo_of, "redo_depth": redo_depth}
                   if type(redo_of) is str and redo_of and type(redo_depth) is int and redo_depth > 0
                   else {})
    # 다시 만들기 상한 뒤의 마지막 작업(context_retention.enqueue_contract_processing). 모델을 부르지
    # 않고 지금 문서를 그대로 둔 채 이 범위의 커서만 넘긴다. 이어지는 작업에도 그대로 넘긴다.
    no_model = payload.get("no_model", False)
    if type(no_model) is not bool or (no_model and (fallback_planner
                                                   or payload.get("partial_message_id") is not None)):
        raise JobFatal("invalid_contract_retention_payload")
    redo_suffix = (f":redo:{redo_of}" if redo_marker else "") + (":skip" if no_model else "")
    async with get_sessionmaker()() as session:
        if not await context_retention.privacy_epoch_matches(session, user_id=uid, privacy_epoch=privacy_epoch):
            raise JobCancelled("contract_privacy_epoch_changed")
        if not await context_retention.enabled_now(session):
            raise JobCancelled("contract_retention_disabled")
        if not await context_retention.adopted_user(session, user_id=uid):
            raise JobCancelled("contract_retention_not_adopted")
        baseline = await context_compat.load(session, uid, generation)
        if ((baseline is None and payload.get("baseline_binding") is not None)
                or (baseline is not None and (payload.get("baseline_binding") != baseline.payload()
                    or baseline.privacy_epoch != privacy_epoch))):
            raise JobCancelled("contract_compat_baseline_changed")
        base = await _retention_state(session, uid, generation)
        if base is None or int(base[0]) != generation:
            raise JobCancelled("stale_contract_generation")
        cursor = _effective_cursor(base, generation, baseline)
        if cursor >= target:
            return JobResult(result_code="already_processed")
        rows = (await session.execute(_RETENTION_MESSAGES, {
            "user_id": uid, "after": cursor, "through": target, "limit": _MAX_SOURCE,
        })).all()
        rows = _logical_source_rows(rows, uid, 4)
        profile = (await session.execute(_PROFILE, {"user_id": uid})).first()
        if profile is None:
            raise JobCancelled("user_deleted")
        previous = await contract_repo.retained_document(session, user_id=uid, generation=generation, compat_baseline=baseline)
        working = previous
        partial_id, partial_offset = payload.get("partial_message_id"), payload.get("partial_offset", 0)
        if partial_id is not None:
            if (type(partial_id) is not int or type(partial_offset) is not int or partial_offset <= 0
                    or not rows or rows[0][0] != partial_id
                    or hashlib.sha256((rows[0][2] or "").encode()).hexdigest() != payload.get("partial_source_hash")
                    or ic.document_hash(json.dumps(previous, ensure_ascii=False, sort_keys=True)) != payload.get("partial_document_hash")):
                raise JobCancelled("contract_partial_source_changed")
            working = await contract_repo.validate_retained_items(session, user_id=uid,
                generation=generation, items=contract_repo.decode_document(payload.get("partial_items")), compat_baseline=baseline)
            await contract_repo.validate_retained_items(session, user_id=uid, generation=generation,
                items=contract_repo.decode_document(payload.get("partial_drafts", [])))
    locale = i18n.resolve(profile[1])
    messages = [_Msg(row[0], row[1], row[2] or "", row[3]) for row in rows]
    if no_model:
        # 모델 입력을 만들지 않으므로 나누기(contract_batch)도 하지 않는다. 읽은 원문 전체를 넘긴다.
        batch = ContractBatch(tuple(messages), len(messages), 0)
    else:
        try:
            batch = contract_batch(messages, previous=working, system=cc.RETENTION_SYSTEM,
                                   start_offset=partial_offset,
                                   retention_enabled=not fallback_planner) if messages else ContractBatch((), 0, 0)
        except cc.CompilerRejection as exc:
            raise JobRetry("contract_background_budget") from exc
    selected = list(batch.messages)
    async with get_sessionmaker()() as session:
        if not await context_retention.privacy_epoch_matches(session, user_id=uid, privacy_epoch=privacy_epoch):
            raise JobCancelled("contract_privacy_epoch_changed")
    partial = batch.next_offset > 0
    if partial:
        boundary = cursor
    elif batch.next_index == len(messages) and len(rows) < _MAX_SOURCE:
        boundary = target
    else:
        boundary = messages[batch.next_index-1].id
    kept, dropped = [], []
    output_fallback = False
    # 마지막 시도: 이번 실패 뒤에 재시도가 없다. 대체 출력 모드는 payload의 남은 모델 호출 수도
    # 본다(호출 직전에 1 줄여 저장하므로, 1이면 이번이 마지막이고 다음 시도는
    # `contract_fallback_attempts_exhausted` JobFatal로 끝난다).
    final_attempt = (job.max_attempts - job.attempt <= 0
                     or (fallback_planner and fallback_available <= 1))
    # 마지막 시도에서 내용 실패로 버린 모델 후보: 이유 → 개수. dead 대신 진행한 흔적이다.
    discarded: dict[str, int] = {}
    document_kept = False
    if not no_model and any(message.sender == "user" for message in selected):
        model_input = _model_input(selected, working)
        try:
            context_retention.assert_input_budget(cc.RETENTION_SYSTEM, model_input,
                                                  output_reserve=_MAX_OUTPUT_TOKENS)
        except context_retention.RetentionError as exc:
            # 모델 호출 전의 결정적 실패다. 일반 예외(unexpected_error)로 두면 일시 장애처럼
            # 보여 dead 뒤 재생성 대상이 되므로 고유 코드로 구분한다.
            raise JobRetry("contract_retention_input_budget") from exc
        if fallback_planner:
            # Persist before inference: a later OFF/cancel/new-row resume must
            # not forget a failed call merely because its physical attempt reset.
            await _persist_output_fallback(job, payload,
                {**payload, "adaptive_output_attempts_remaining": fallback_available-1})
        result = await llm.generate(
            cc.RETENTION_SYSTEM, model_input,
            model=settings.model_utility, reasoning_effort="none", max_tokens=_MAX_OUTPUT_TOKENS,
            ledger=usage_ledger.LedgerContext(lane=usage_ledger.LANE_BACKGROUND,
                purpose="contract_compile", user_id=uid, job_id=job.id, attempt=job.attempt),
        )
        parse_output = True
        if result.finish_reason == "length":
            enlarged = sum(len(message.content.encode("utf-8")) for message in selected) > checkpoint.BACKGROUND_SEGMENT_BYTES
            if not fallback_planner and enlarged and job.max_attempts - job.attempt > 0:
                output_fallback = True
                fallback_available = job.max_attempts-job.attempt
                await _persist_output_fallback(job, payload, {**payload,
                    "adaptive_output_fallback": True,
                    "adaptive_output_attempts_remaining": fallback_available})
            elif not final_attempt:
                raise JobRetry("contract_retention_length")
            else:
                # 잘린 출력은 읽지 않는다(부분 JSON을 해석·병합·발행하지 않음).
                parse_output = False
                discarded["length"] = 0
        if not output_fallback and parse_output:
            try:
                llm.require_completed_text(result.text, result.finish_reason)
            except llm.IncompleteResponseError as exc:
                # 빈 응답·content_filter 등. 출력 내용은 오류·로그에 넣지 않는다.
                if not final_attempt:
                    raise JobRetry("contract_retention_incomplete") from exc
                discarded["incomplete"] = 0
            else:
                try:
                    kept, dropped = cc.parse(result.text,
                        user_message_ids={message.id for message in selected if message.sender == "user"},
                        source_messages=selected, existing_items=working)
                except cc.CompilerRejection as exc:
                    if not final_attempt:
                        raise JobRetry("contract_retention_schema") from exc
                    discarded["schema"] = 0
                    kept, dropped = [], []
                if dropped:
                    if not final_attempt:
                        raise JobRetry("contract_retention_evidence")
                    # 마지막 시도: 검증된 항목만 쓰고 커서를 넘긴다. 여기서 dead가 되면 같은 범위의
                    # 새 작업이 생기지 않아 이 사용자의 약속 정리와 요약 게시가 계속 멈춘다.
                    # 버린 항목은 저장하지 않으므로 검증 안 된 약속이 들어가지는 않는다.
                    discarded["evidence"] = len(dropped)
    if not output_fallback:

        def merge_candidates(candidates):
            ordinary = [candidate for candidate in candidates if candidate.operation == "end" or candidate.directive.kind not in HIGH_IMPACT]
            drafts = [candidate for candidate in candidates if candidate.operation != "end" and candidate.directive.kind in HIGH_IMPACT]
            # 닫힌 긍정 종료는 모델 누락([]) 뒤 커서만 전진하여 옛 선호가 부활하지 않게 한다.
            setters = [candidate for candidate in ordinary if candidate.operation != "end"]
            prospective = merge_retained_items(working, setters, generation=generation)
            model_ends = [candidate for candidate in ordinary if candidate.operation == "end"]
            ended_keys = {candidate.ends_item_key for candidate in model_ends}
            supplementary = [candidate for candidate in cc.explicit_scope_end_candidates(selected, prospective)
                             if candidate.ends_item_key not in ended_keys]
            ordinary = sorted([*ordinary, *supplementary],
                              key=lambda candidate: (candidate.source_message_id, candidate.operation == "end"))
            merged = merge_retained_items(working, ordinary, generation=generation)
            if baseline is not None:
                merged = contract_repo.apply_legacy_ends(merged, selected)
            return ordinary, drafts, merged, render_items(merged)

        def render_items(items):
            active = contract_repo.active_retained_items(items,
                activity_date=selected[-1].activity_date if selected else None)
            return ic.render_document([contract_repo.directive_from_item(item) for item in active],
                                      language=locale) if active else ""

        def checked_merge(candidates):
            """(병합 결과, None) 또는 (None, (오류 코드, 원인 예외))."""
            try:
                values = merge_candidates(candidates)
            except (cc.CompilerRejection, ic.ContractViolation) as exc:
                return None, ("contract_retention_merge", exc)
            if len(values[3].encode()) > 2_048:
                return None, ("contract_retention_budget", None)
            return values, None

        if no_model:
            # 상한 뒤 마지막 작업: 모델 후보 없이 규칙 기반 종료(사용자가 직접 끝낸 대화 선호)만 적용한다.
            # 모델은 부르지 않는다. 그것도 실패하면 문서는 그대로 두고 커서만 넘긴다.
            values, failure = checked_merge([])
            if failure is not None:
                values, failure = ([], [], working, render_items(working)), None
                document_kept = True
        else:
            values, failure = checked_merge(kept)
        if failure is not None:
            code, cause = failure
            if not final_attempt:
                raise JobRetry(code) from cause
            # 마지막 시도: 모델 후보를 모두 버리고 기존 문서 + 규칙 기반 종료만으로 다시 계산한다.
            discarded[code.removeprefix("contract_retention_")] = len(kept)
            values, failure = checked_merge([])
            if failure is not None:
                # 그래도 안 되면 문서는 그대로 두고 커서만 넘긴다(모델을 다시 부르지 않는다).
                # 크기 검사는 하지 않는다: 이미 검증된 문서를 키우지 않고 그대로 다시 쓴다.
                values = ([], [], working, render_items(working))
                document_kept = True
        ordinary, drafts, merged, rendered = values
    if discarded:
        _log.warning("계약 마지막 시도 내용 실패 — 모델 후보 폐기 후 진행 job=%s attempt=%s discarded=%s document_kept=%s",
                     job.id, job.attempt, discarded, document_kept)
    if no_model:
        # 개수와 식별자만 남긴다(원문 없음).
        _log.warning("contract_redo_cap_skip job=%s redo_of=%s redo_depth=%s messages=%d through=%s",
                     job.id, redo_of if redo_marker else None, redo_depth if redo_marker else None,
                     len(selected), boundary)

    async def _apply(session) -> None:
        if not await context_retention.privacy_epoch_matches(session, user_id=uid,
                privacy_epoch=privacy_epoch, lock=True):
            return
        if not await context_retention.enabled_now(session, lock=True):
            return
        if not await context_retention.adopted_user(session, user_id=uid, lock=True):
            return
        current_baseline = await context_compat.load(session, uid, generation)
        if current_baseline != baseline:
            return
        fresh = await _retention_state(session, uid, generation, lock=True)
        if fresh is None or int(fresh[0]) != generation:
            return

        # 새로 읽은 상태의 실제 커서. 처리기 앞부분(cursor)과 같은 규칙으로 계산해야 한다:
        # 선호 세대가 다르면 0이고, 호환 기준선이 있으면 그 경계까지 올린다.
        fresh_cursor = _effective_cursor(fresh, generation, baseline)

        async def restart_current() -> None:
            from app.services import jobs
            # after_message_id: 대기 쪽(enqueue_contract_processing)이 이 작업을 현재 커서의
            # 작업으로 찾는 조건이다. 재생성·모델 없음 표시는 이어지는 작업에도 그대로 넘긴다.
            restart_payload = {"retention_enabled": True, "memory_generation": generation,
                               "through_message_id": target, "after_message_id": fresh_cursor,
                               "privacy_epoch": privacy_epoch, **redo_marker}
            if no_model:
                restart_payload["no_model"] = True
            if baseline is not None:
                restart_payload["baseline_binding"] = baseline.payload()
            if fallback_planner or output_fallback:
                restart_payload["adaptive_output_fallback"] = True
                restart_payload["adaptive_output_attempts_remaining"] = fallback_available
            await jobs.enqueue(session, queue=job.queue, job_type=JOB_CONTRACT_COMPILE,
                user_id=uid, dedup_key=f"contract-retention:{uid}:{generation}:{fresh_cursor}:{target}:after:{job.id}",
                payload=restart_payload,
                max_attempts=fallback_available if fallback_planner or output_fallback else 3)

        if tuple(fresh) != tuple(base):
            # 다른 정상 작업이 앞 구간을 처리했어도 남은 목표를 새 커서에서 이어 간다.
            if fresh_cursor < target:
                await restart_current()
            return
        # 모델 실행 중 원문이 바뀌거나 safety로 재분류되면 처리 완료로 넘기지 않는다.
        current = (await session.execute(_RETENTION_MESSAGES, {
            "user_id": uid, "after": cursor, "through": target, "limit": _MAX_SOURCE,
        })).all()
        current = _logical_source_rows(current, uid, 4)
        if [tuple(row) for row in current] != [tuple(row) for row in rows]:
            await restart_current()
            return
        latest_previous = await contract_repo.retained_document(session, user_id=uid, generation=generation, compat_baseline=baseline)
        if latest_previous != previous:
            await restart_current()
            return
        if output_fallback:
            from app.services import jobs
            await jobs.enqueue(session, queue=job.queue, job_type=JOB_CONTRACT_COMPILE,
                user_id=uid, dedup_key=f"contract-output-fallback:{job.id}",
                payload={**payload, "adaptive_output_fallback": True,
                         "adaptive_output_attempts_remaining": fallback_available},
                max_attempts=job.max_attempts-job.attempt)
            return  # Truncated text is neither parsed, merged nor published.
        draft_items = [*(payload.get("partial_drafts") or []),
                       *[_retention_item(candidate, generation) for candidate in drafts]]
        if partial:
            from app.services import jobs
            first = messages[0]
            source_hash = hashlib.sha256(first.content.encode()).hexdigest()
            previous_hash = ic.document_hash(json.dumps(previous, ensure_ascii=False, sort_keys=True))
            next_payload = {**payload, "partial_message_id": first.id, "partial_offset": batch.next_offset,
                "partial_source_hash": source_hash, "partial_items": merged, "partial_drafts": draft_items,
                "partial_document_hash": previous_hash}
            if fallback_planner:
                next_payload["adaptive_output_attempts_remaining"] = fallback_available
            await jobs.enqueue(session, queue=job.queue, job_type=JOB_CONTRACT_COMPILE,
                user_id=uid, dedup_key=f"contract-retention:{uid}:{generation}:{cursor}:{target}:source:{first.id}:{source_hash}:{previous_hash}:{batch.next_offset}{redo_suffix}",
                payload=next_payload,
                max_attempts=fallback_available if fallback_planner else 3)
            return  # 원문 전체를 읽기 전 커서 전진·중간 선호 발행을 하지 않는다.
        if retention_release.binding() is not None:
            doc = json.dumps(merged, ensure_ascii=False, sort_keys=True)
            await retention_release.store_contract(session, user_id=uid, generation=generation,
                locale=locale, items=merged, rendered=rendered, digest=ic.document_hash(doc),
                through=boundary, draft_items=draft_items, after=cursor,
                source_rows=[row for row in current if cursor < row[0] <= boundary])
        else:
            stored_items, validation = (contract_repo.compatibility_metadata(merged, baseline,
                after=cursor, through=boundary,
                source_rows=[row for row in current if cursor < row[0] <= boundary])
                if baseline is not None else (merged, None))
            for items, text_render, publish in ((stored_items, rendered, True),
                    (draft_items, "", False)):
                if not items and not (publish and baseline is not None):
                    continue
                doc = json.dumps(items, ensure_ascii=False, sort_keys=True)
                digest = ic.document_hash(doc)
                if publish and baseline is None and await contract_repo.has_same_document(session, user_id=uid, document_hash=digest):
                    continue
                version = int(await session.scalar(_NEXT_VERSION, {"user_id": uid, "locale": locale}))
                cid = await session.scalar(_RETENTION_INSERT, {
                    "user_id": uid, "version": version, "locale": locale, "doc": doc,
                    "rendered": text_render, "hash": digest, "through": boundary,
                    "validation": json.dumps(validation, ensure_ascii=False, default=str) if publish and validation is not None else None,
                })
                for item in items:
                    directive = contract_repo.directive_from_item(item)
                    await session.execute(_INSERT_ITEM, {
                        "contract_id": cid, "user_id": uid, "item_key": item["item_key"],
                        "section": _SECTION[directive.kind], "value": json.dumps(item, ensure_ascii=False),
                        "rendered": "" if item.get("status") == "ended" else ic.render(directive, language=locale),
                        "source_message_id": item["source_message_id"],
                        "item_status": "superseded" if item.get("status") == "ended" else "active",
                    })
                if publish:
                    await contract_repo.publish(session, contract_id=cid, user_id=uid, locale=locale)
            await session.execute(_RETENTION_ADVANCE, {
                "user_id": uid, "generation": generation, "through": boundary,
            })
        if boundary < target:
            from app.services import jobs
            next_payload = {key: value for key, value in payload.items() if not key.startswith("partial_")}
            if fallback_planner:
                next_payload["adaptive_output_attempts_remaining"] = fallback_available
            await jobs.enqueue(session, queue=job.queue, job_type=JOB_CONTRACT_COMPILE,
                user_id=uid, dedup_key=f"contract-retention:{uid}:{generation}:{boundary}:{target}{redo_suffix}",
                payload={**next_payload, "after_message_id": boundary},
                max_attempts=fallback_available if fallback_planner else 3)
    detail = {
        "kept": 0 if output_fallback else len(ordinary), "draft": 0 if output_fallback else len(drafts),
        "through": cursor if output_fallback else boundary,
        "adaptive_output_fallback": output_fallback}
    if discarded:
        # result_code는 "ok"로 둔다(이어받기 조건). 버린 이유·개수만 남긴다(원문 없음).
        detail.update(discarded=discarded, document_kept=document_kept)
    if no_model:
        detail.update(skipped_after_redo_cap=True, skipped_messages=len(selected),
                      document_kept=document_kept)
    return JobResult(result_code="ok", result_detail=detail, apply_domain=_apply)


consumer.register(JOB_CONTRACT_COMPILE, handle_contract_compile)
