"""프롬프트 조립 v2 — 순서 보존 segment와 byte 단위 serializer.

기억 재설계 9단계(docs/ARCHITECTURE-capi.md 3장).

**왜 기존 경로를 못 쓰나**
현행 `chat._build_system()`은 checkpoint와 current-state를 system 앞쪽에 미리 합치고,
`llm.to_openai_messages()`는 단일 system을 맨 앞에 고정한다. 둘 다 "휘발 값은 최근 원문 **뒤**"라는
새 순서를 표현할 수 없다. assembler만 바꾸고 serializer가 다시 합치면 **반쪽 전환**이고, 그건
cutover 실패다(14.3절). 그래서 이 모듈이 조립과 직렬화를 함께 소유한다.

**고정 순서** — 이게 이 모듈의 전부다.

    stable system  →  recent user/assistant  →  current-context system  →  current user  →  tool result

매 턴 달라지는 값(서버 snapshot·mem0·checkpoint)을 최근 원문 **앞**에 두면 그 뒤의 append-only
대화가 전부 cache miss가 된다. 최근 원문은 append-only라 그 자체가 캐시 가능한 프리픽스다.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# ─────────────────────────────────────────────────────────────
# cache class — 이 값이 순서를 정한다. 숫자가 곧 정렬 키다.
# ─────────────────────────────────────────────────────────────
class CacheClass(int, Enum):
    """캐시 수명. 낮을수록 오래 안 변하고 앞에 온다."""

    STABLE = 0        # 페르소나·안전규칙·계약·관계 stage — version/hash 바뀔 때만
    APPEND_ONLY = 1   # 최근 원문 대화 — 뒤로만 늘어난다
    CURRENT = 2       # 서버 snapshot·checkpoint·mem0 — 매 턴 달라진다
    INPUT = 3         # 이번 사용자 입력
    TOOL = 4          # 2차 호출의 도구 결과


class SegmentKind(str, Enum):
    PERSONA = "persona"
    SAFETY = "safety"
    CONTRACT = "contract"
    RELATIONSHIP = "relationship"
    RECENT = "recent"
    SERVER_SNAPSHOT = "server_snapshot"
    CHECKPOINT = "checkpoint"
    PENDING_BRIDGE = "pending_bridge"
    MEMORY = "memory"
    CURRENT_INPUT = "current_input"
    TOOL_RESULT = "tool_result"


# kind → 그 kind가 속해야 하는 cache class. 조립할 때 코드가 강제한다.
_KIND_CLASS: dict[SegmentKind, CacheClass] = {
    SegmentKind.PERSONA: CacheClass.STABLE,
    SegmentKind.SAFETY: CacheClass.STABLE,
    SegmentKind.CONTRACT: CacheClass.STABLE,
    SegmentKind.RELATIONSHIP: CacheClass.STABLE,
    SegmentKind.RECENT: CacheClass.APPEND_ONLY,
    SegmentKind.SERVER_SNAPSHOT: CacheClass.CURRENT,
    SegmentKind.CHECKPOINT: CacheClass.CURRENT,
    SegmentKind.PENDING_BRIDGE: CacheClass.CURRENT,
    SegmentKind.MEMORY: CacheClass.CURRENT,
    SegmentKind.CURRENT_INPUT: CacheClass.INPUT,
    SegmentKind.TOOL_RESULT: CacheClass.TOOL,
}

# untrusted — 이 안의 지시는 실행하지 않는다. stable system이 그 규칙을 갖는다.
UNTRUSTED_KINDS = frozenset(
    {SegmentKind.MEMORY, SegmentKind.CHECKPOINT, SegmentKind.PENDING_BRIDGE,
     SegmentKind.TOOL_RESULT}
)


class PromptOrderError(ValueError):
    """순서·분류 계약 위반 — 조용히 재정렬하지 않고 실패시킨다."""


@dataclass(frozen=True, slots=True)
class PromptSegment:
    kind: SegmentKind
    role: str          # system | developer | user | assistant | tool
    content: str

    @property
    def cache_class(self) -> CacheClass:
        return _KIND_CLASS[self.kind]

    @property
    def untrusted(self) -> bool:
        return self.kind in UNTRUSTED_KINDS


def assemble(segments: list[PromptSegment]) -> list[PromptSegment]:
    """cache class 순으로 **안정 정렬**한다. 같은 class 안에서는 준 순서를 지킨다.

    호출측이 순서를 틀려도 여기서 바로잡되, 분류 자체가 틀린 건(예: mem0를 STABLE로 선언)
    `_KIND_CLASS`가 강제하므로 애초에 불가능하다.
    """
    if not segments:
        raise PromptOrderError("빈 프롬프트는 만들 수 없다")
    ordered = sorted(segments, key=lambda s: s.cache_class.value)
    _validate(ordered)
    return ordered


def _validate(ordered: list[PromptSegment]) -> None:
    classes = [s.cache_class.value for s in ordered]
    if classes != sorted(classes):  # pragma: no cover - sorted 보장
        raise PromptOrderError("cache class 순서가 어긋났다")
    if not any(s.cache_class is CacheClass.STABLE for s in ordered):
        raise PromptOrderError("stable system이 없다 — 페르소나·안전 규칙이 빠졌다")
    # 휘발 값이 최근 원문보다 앞에 오면 그 뒤 대화가 전부 cache miss가 된다.
    last_recent = max(
        (i for i, s in enumerate(ordered) if s.cache_class is CacheClass.APPEND_ONLY),
        default=-1,
    )
    first_current = min(
        (i for i, s in enumerate(ordered) if s.cache_class is CacheClass.CURRENT),
        default=len(ordered),
    )
    if last_recent > first_current:
        raise PromptOrderError("휘발 컨텍스트가 최근 원문 앞에 있다 — 프리픽스 캐시가 깨진다")


def stable_prefix(ordered: list[PromptSegment]) -> list[PromptSegment]:
    """캐시 identity 계산 대상. 이 구간의 byte가 같아야 provider 캐시가 산다."""
    return [s for s in ordered if s.cache_class is CacheClass.STABLE]


def to_openai_messages(ordered: list[PromptSegment]) -> list[dict]:
    """segment → OpenAI messages. **합치지 않고 순서를 그대로 내보낸다.**

    기존 serializer는 system을 하나로 합쳐 맨 앞에 고정했다. 그러면 current-context가 최근 원문
    뒤에 올 수 없어 새 순서를 표현하지 못한다. 여기서는 인접한 **stable system만** 하나로 합치고
    (프리픽스 byte 안정성), 나머지는 준 순서대로 개별 메시지로 낸다.
    """
    out: list[dict] = []
    stable_buf: list[str] = []
    for seg in ordered:
        if seg.cache_class is CacheClass.STABLE:
            stable_buf.append(seg.content)
            continue
        if stable_buf:
            out.append({"role": "system", "content": "\n\n".join(stable_buf)})
            stable_buf = []
        out.append({"role": seg.role, "content": seg.content})
    if stable_buf:  # stable만 있는 경우
        out.append({"role": "system", "content": "\n\n".join(stable_buf)})
    return out


def prompt_bytes(ordered: list[PromptSegment]) -> int:
    """shadow 비교용 — 직렬화 결과의 UTF-8 byte 수."""
    return sum(len(m["content"].encode("utf-8")) for m in to_openai_messages(ordered))
