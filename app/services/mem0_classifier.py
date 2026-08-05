"""consolidation classifier — 고정 스키마 판정 파서.

기억 재설계(docs/ARCHITECTURE-capi.md 5.3절).

모델은 `independent | duplicate | supersedes | ambiguous`와 **비교 대상 id만** 낸다.
자유문 판정과 존재하지 않는 id는 거부한다 — 그걸 받으면 validator가 검사할 대상 자체가 오염된다.

**신규↔신규와 신규↔기존을 한 번에 batch 판정한다.** memory마다 top-k와 classifier를 반복하면
호출 수가 후보 수만큼 늘고 판정이 서로 모순될 수 있다.
"""
from __future__ import annotations

import json
import uuid

from app.services.mem0_consolidation import Edge, Verdict

# v2: 근사 중복 duplicate · 부정이 긍정을 supersedes(2026-08-06 감사 지적).
CLASSIFIER_VERSION = "mem0-classifier-v2"
MAX_EXISTING_CANDIDATES = 12
MAX_OUTPUT_TOKENS = 900


class ClassifierSchemaError(ValueError):
    """판정 출력이 계약을 어겼다 — 그래프 전체를 쓰지 않는다."""


def build_system() -> str:
    return (
        "너는 두 기억이 어떤 관계인지 판정한다. 설명하지 말고 JSON 객체 하나만 출력한다.\n\n"
        "[출력]\n"
        '{"edges":[{"subject":"<id>","target":"<id>","verdict":"..."}]}\n\n'
        "[verdict]\n"
        "- independent: 서로 다른 사실. 둘 다 유지\n"
        "- duplicate: 같은 내용을 다시 말한 것\n"
        "- supersedes: subject가 target을 **정정**한 더 새로운 값\n"
        "- ambiguous: 상충할 수 있으나 어느 쪽이 현재인지 확정 불가\n\n"
        "[규칙]\n"
        "- id는 입력에 있는 것만 쓴다. 새로 만들지 않는다.\n"
        "- 관계가 없으면 edge를 만들지 않는다. 억지로 잇지 않는다.\n"
        "- 확신이 없으면 ambiguous로 둔다. 단정이 더 위험하다.\n"
        "- **같은 뜻을 조금 다르게 말한 것은 duplicate다.** '~하려고 한다'와 '~하고 싶다'처럼 "
        "표현만 다르면 둘 다 남기지 않는다.\n"
        "- **부정은 긍정을 정정한다.** '산책을 안 했다'는 '산책을 갔다'를 supersedes다. "
        "같은 대상에 대한 반대 진술이면 나중 것이 앞선 것을 대체한다.\n"
        "- 설명도 코드펜스도 붙이지 말고 JSON만 출력한다."
    )


def render_pairs(new_items: list[tuple[uuid.UUID, str]], existing: list[tuple[uuid.UUID, str]]) -> str:
    """판정 입력. 신규와 기존을 라벨로 구분해 한 번에 준다."""
    lines = ["[신규]"]
    lines += [f"{i}: {t}" for i, t in new_items]
    if existing:
        lines.append("")
        lines.append("[기존]")
        lines += [f"{i}: {t}" for i, t in existing]
    return "\n".join(lines)


def parse(text: str, *, known_ids: set[uuid.UUID]) -> list[Edge]:
    """판정 출력 → edge 목록. 계약 위반은 전량 거부한다."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
    try:
        obj = json.loads(raw)
    except ValueError as e:
        raise ClassifierSchemaError(f"JSON 파싱 실패: {e}") from e
    if not isinstance(obj, dict):
        raise ClassifierSchemaError("최상위가 객체가 아니다")
    items = obj.get("edges")
    if not isinstance(items, list):
        raise ClassifierSchemaError("edges가 배열이 아니다")

    out: list[Edge] = []
    for item in items:
        if not isinstance(item, dict):
            raise ClassifierSchemaError(f"edge가 객체가 아니다: {item!r}")
        try:
            subject = uuid.UUID(str(item["subject"]))
            target = uuid.UUID(str(item["target"]))
        except (KeyError, ValueError, TypeError) as e:
            raise ClassifierSchemaError(f"id가 UUID가 아니다: {item!r}") from e
        verdict_raw = item.get("verdict")
        try:
            verdict = Verdict(verdict_raw)
        except ValueError as e:
            # 자유문 판정 — 받아주면 validator가 검사할 대상이 오염된다.
            raise ClassifierSchemaError(f"허용 밖 verdict: {verdict_raw!r}") from e
        if subject not in known_ids or target not in known_ids:
            raise ClassifierSchemaError(f"입력에 없는 id 참조: {subject} → {target}")
        out.append(Edge(subject=subject, target=target, verdict=verdict))
    return out
