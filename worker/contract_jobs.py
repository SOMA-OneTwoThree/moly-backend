"""interaction contract 추출·발행 잡 (6.3절).

**캐피의 단일 추측은 published contract를 직접 바꾸지 못한다.** 그래서 이 잡은 후보를
만들되, 영향이 큰 항목은 draft로 남기고 낮은 것만 publish한다.

영향이 큰 것 = 경계(피할 주제·표현)와 관계 정의. 이건 잘못 걸면 캐피가 사용자가 하지 않은
약속 때문에 말을 못 하게 된다. 나머지(호칭·말투·위로 방식)는 6.3절대로 되묻지 않고 바로
publish한다 — 매번 "이렇게 해줄까?"로 확인하면 페르소나의 질문 절약 규칙과 충돌하고,
사용자가 확답 없이 화제를 넘기면 합의가 또 사라진다.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import text

from app.config import settings
from app.core.db import get_sessionmaker
from app.services import contract_compiler as cc
from app.services import contract_repo
from app.services import interaction_contract as ic
from app.services import llm, usage_ledger
from app.services.jobs import ClaimedJob
from worker import consumer
from worker.consumer import JobCancelled, JobFatal, JobResult, JobRetry

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
SELECT id, sender, content FROM (
  SELECT id, sender, content FROM messages
  WHERE user_id = :user_id AND kind = 'normal' AND id > :after
  ORDER BY id DESC LIMIT :limit
) recent
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
        'explicit_user', :source_message_id, 'active')
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


async def handle_contract_compile(job: ClaimedJob) -> JobResult:
    if job.user_id is None:
        raise JobFatal("missing_user")
    uid = job.user_id
    after = int((job.payload or {}).get("after_message_id", 0))

    async with get_sessionmaker()() as session:
        rows = (await session.execute(
            _MESSAGES, {"user_id": uid, "after": after, "limit": _MAX_SOURCE}
        )).all()
        profile = (await session.execute(_PROFILE, {"user_id": uid})).first()
    if not rows:
        raise JobCancelled("no_source_messages")

    locale = (profile[1] if profile else None) or "ko"
    msgs = [_Msg(r[0], r[1], r[2] or "") for r in rows]
    user_ids = {m.id for m in msgs if m.sender == "user"}

    try:
        result = await llm.generate(
            cc.SYSTEM,
            [{"role": "user", "content": cc.render_conversation(msgs)}],
            model=settings.model_utility, max_tokens=_MAX_OUTPUT_TOKENS,
            ledger=usage_ledger.LedgerContext(
                lane=usage_ledger.LANE_BACKGROUND, purpose="contract_compile",
                user_id=uid, job_id=job.id, attempt=job.attempt,
            ),
        )
    except Exception as e:  # noqa: BLE001  provider 장애 — 재시도
        raise JobRetry("contract_llm_failed") from e

    try:
        kept, dropped = cc.parse(result.text, user_message_ids=user_ids)
    except cc.CompilerRejection as e:
        raise JobRetry("contract_schema") from e
    if dropped:
        _log.info("계약 후보 폐기 %d건 — job=%s", len(dropped), job.id)
    if not kept:
        return JobResult(result_code="no_candidates")

    doc = _document(kept)
    digest = ic.document_hash(doc)
    rendered = ic.render_document([c.directive for c in kept])
    auto_publish = not any(c.directive.kind in HIGH_IMPACT for c in kept)

    async def _apply(session) -> None:
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
                "rendered": ic.render(c.directive),
                "source_message_id": c.source_message_id,
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
    __slots__ = ("id", "sender", "content")

    def __init__(self, id: int, sender: str, content: str):
        self.id, self.sender, self.content = id, sender, content


consumer.register(JOB_CONTRACT_COMPILE, handle_contract_compile)
