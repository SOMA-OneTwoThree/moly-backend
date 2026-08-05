"""contract backfill — 기존 대화에서 명시적 합의를 뽑아 **draft로만** 만든다(15장 6번).

문서 요구: "명시적 user 말투·호칭·경계만 candidate로 만들고 자동 publish 결과를 사람 검토·
fixture와 대조한다."

**publish하지 않는다.** 전부 `status='draft'`로 넣고 사람이 보고 올린다. 잘못 만든 항목은
stable prefix에 실려 매 턴 캐피의 행동을 바꾸는데, 사용자는 자기가 그런 말을 한 적 없다는
것조차 모른다. 그래서 자동 publish는 이 스크립트의 기능이 아니다.

기본은 dry-run이며 **뽑은 것과 버린 것을 함께 출력한다.** 버린 게 0이면 필터가 동작하지
않는다는 신호다.

사용:
    PYTHONPATH=. uv run python scripts/backfill_interaction_contracts.py --limit 1
    PYTHONPATH=. uv run python scripts/backfill_interaction_contracts.py --users <uuid> --yes
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from sqlalchemy import text

from app.config import settings
from app.core.db import get_sessionmaker
from app.services import contract_compiler as cc
from app.services import interaction_contract as ic
from app.services import llm, usage_ledger
from db.envfile import announce, load_conn, split_env_arg

# 한 번에 읽는 원문 상한. 넘으면 앞이 잘려 부분 이력을 전체인 양 보게 된다.
_MAX_SOURCE = 300
_MAX_OUTPUT_TOKENS = 2_000

_CANDIDATE_USERS = text("""
SELECT DISTINCT m.user_id
FROM messages m
WHERE m.kind = 'normal' AND m.sender = 'user'
GROUP BY m.user_id
HAVING count(*) >= 5
ORDER BY m.user_id
LIMIT :limit
""")

_MESSAGES = text("""
SELECT id, sender, content
FROM messages
WHERE user_id = :user_id AND kind = 'normal'
ORDER BY id
LIMIT :limit
""")

_PROFILE = text("SELECT nickname, language FROM profiles WHERE id = :user_id")

_INSERT_CONTRACT = text("""
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

_NEXT_VERSION = text("""
SELECT COALESCE(max(version), 0) + 1
FROM user_interaction_contracts WHERE user_id = :user_id AND locale = :locale
""")

# kind → contract item의 section. 스키마 CHECK와 같아야 한다.
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


class _Msg:
    __slots__ = ("id", "sender", "content")

    def __init__(self, id: int, sender: str, content: str):
        self.id, self.sender, self.content = id, sender, content


def _item_key(d: ic.Directive) -> str:
    """같은 합의가 두 번 들어가지 않게 하는 키. 값이 바뀌면 다른 항목이다."""
    target = d.target_literal or d.target_tag or ""
    return f"{d.kind.value}:{d.action.value}:{d.condition.value}:{target}"


async def _compile_for_user(session, uid: uuid.UUID) -> tuple[list, list[str], str]:
    rows = (await session.execute(
        _MESSAGES, {"user_id": uid, "limit": _MAX_SOURCE}
    )).all()
    profile = (await session.execute(_PROFILE, {"user_id": uid})).first()
    locale = (profile[1] if profile else None) or "ko"
    if not rows:
        return [], ["원문 없음"], locale

    messages = [_Msg(r[0], r[1], r[2] or "") for r in rows]
    user_ids = {m.id for m in messages if m.sender == "user"}
    result = await llm.generate(
        cc.SYSTEM,
        [{"role": "user", "content": cc.render_conversation(messages)}],
        model=settings.model_utility,
        max_tokens=_MAX_OUTPUT_TOKENS,
        ledger=usage_ledger.LedgerContext(
            lane=usage_ledger.LANE_BACKGROUND, purpose="contract_compile", user_id=uid,
        ),
    )
    try:
        kept, dropped = cc.parse(result.text, user_message_ids=user_ids)
    except cc.CompilerRejection as e:
        return [], [f"추출 실패: {e}"], locale
    return kept, dropped, locale


async def main(env: str | None, users: list[str], limit: int, apply: bool) -> int:
    dsn = load_conn(env)
    announce(env, dsn)
    maker = get_sessionmaker()

    async with maker() as session:
        if users:
            targets = [uuid.UUID(u) for u in users]
        else:
            targets = [r[0] for r in (await session.execute(
                _CANDIDATE_USERS, {"limit": limit}
            )).all()]

    if not targets:
        print("\n대상 사용자가 없다(사용자 발화 5건 이상).")
        return 0

    print(f"\n대상 {len(targets)}명 — {'반영(draft 생성)' if apply else 'dry-run'}")
    total_kept = total_dropped = 0

    for uid in targets:
        async with maker() as session:
            kept, dropped, locale = await _compile_for_user(session, uid)
            total_kept += len(kept)
            total_dropped += len(dropped)
            print(f"\n[{str(uid)[:8]}…] 후보 {len(kept)}건 / 버림 {len(dropped)}건")
            for c in kept:
                print(f"  · {ic.render(c.directive)}   (#{c.source_message_id} {c.rationale})")
            for reason in dropped[:5]:
                print(f"  ✕ {reason}")

            if apply and kept:
                version = int(await session.scalar(
                    _NEXT_VERSION, {"user_id": uid, "locale": locale}
                ))
                rendered = ic.render_document([c.directive for c in kept])
                doc = json.dumps(
                    [{
                        "kind": c.directive.kind.value,
                        "action": c.directive.action.value,
                        "condition": c.directive.condition.value,
                        "polarity": c.directive.polarity.value,
                        "target_tag": c.directive.target_tag,
                        "target_literal": c.directive.target_literal,
                        "source_message_id": c.source_message_id,
                    } for c in kept],
                    ensure_ascii=False,
                )
                cid = await session.scalar(_INSERT_CONTRACT, {
                    "user_id": uid, "version": version, "locale": locale,
                    "doc": doc, "rendered": rendered,
                    "hash": f"{cc.COMPILER_VERSION}:{abs(hash(rendered)):x}",
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
                await session.commit()
                print(f"  → draft v{version} 생성 ({len(kept)}항목)")
            else:
                await session.rollback()

    print("\n" + "=" * 60)
    print(f"후보 {total_kept}건, 버림 {total_dropped}건.")
    if total_dropped == 0 and total_kept:
        print("⚠️ 버린 게 0건이다 — 필터가 동작하는지 의심할 것.")
    if apply:
        print("전부 **draft**다. 검토 후 사람이 publish한다 — 이 스크립트는 publish하지 않는다.")
    else:
        print("dry-run이다. 실제 draft 생성은 --yes 를 붙인다.")
    return 0


_env, _rest = split_env_arg(sys.argv[1:])
_p = argparse.ArgumentParser()
_p.add_argument("--users", default="")
_p.add_argument("--limit", type=int, default=1)
_p.add_argument("--yes", action="store_true")
_a = _p.parse_args(_rest)
raise SystemExit(asyncio.run(main(
    _env, [u for u in _a.users.split(",") if u], _a.limit, _a.yes,
)))
