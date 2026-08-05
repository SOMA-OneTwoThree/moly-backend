"""사용자별 interaction contract — bounded directive 스키마와 안전 렌더(6.2절).

**이 모듈의 목적은 사용자 문장을 프롬프트에 넣지 않는 것이다.**

사용자가 "앞으로 반말로 해줘"라고 하면 그 합의는 계속 지켜져야 한다. 그런데 그 문장을 그대로
stable prefix에 넣으면, 사용자가 쓴 임의의 텍스트가 매 턴 **명령 위치**에서 모델에게 전달된다.
그 자리에 "이전 지시를 무시하고…"가 들어오면 페르소나와 안전 규칙이 통째로 흔들린다.

그래서 raw 문장은 source message로만 두고, 여기서는 **닫힌 스키마의 typed 값**으로만 저장한다.
자유 문자열이 들어갈 수 있는 자리는 `target_literal` 하나뿐이며, 그것도:
 · NFKC 정규화 · 단일행 · 최대 64 grapheme
 · 제어문자·bidi·Markdown/XML delimiter·role/tool token 금지
 · 서버 template의 **인용된 데이터 슬롯**에만 escape해 렌더 (명령 위치 금지)

캐피 정체성·안전 규칙을 바꾸려는 요청은 애초에 contract 후보가 될 수 없다.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

MAX_LITERAL_GRAPHEMES = 64


class Kind(str, Enum):
    ADDRESS = "address"
    RESPONSE_STYLE = "response_style"
    COMFORT = "comfort"
    TOPIC_BOUNDARY = "topic_boundary"
    EXPRESSION_BOUNDARY = "expression_boundary"
    RELATIONSHIP_DEFINITION = "relationship_definition"
    DURABLE_BEHAVIOR = "durable_behavior"
    CUSTOM_PREFERENCE = "custom_preference"


class Action(str, Enum):
    USE = "use"
    AVOID = "avoid"
    PREFER = "prefer"
    ASK_BEFORE = "ask_before"
    LISTEN_BEFORE = "listen_before"
    DO_NOT_ASSUME = "do_not_assume"
    HONOR_PREFERENCE = "honor_preference"


class Condition(str, Enum):
    ALWAYS = "always"
    WHEN_DISTRESSED = "when_distressed"
    WHEN_ASKING_ADVICE = "when_asking_advice"
    WHEN_TOPIC_TAG = "when_topic_tag"
    CUSTOM_TRIGGER = "custom_trigger"


class Polarity(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


# kind별 허용 action. 조합을 코드 테이블로 강제한다 — 자유 조합을 허용하면
# "관계 정의를 use한다" 같은 의미 없는(그러나 렌더는 되는) 항목이 생긴다.
ALLOWED_ACTIONS: dict[Kind, frozenset[Action]] = {
    Kind.ADDRESS: frozenset({Action.USE, Action.AVOID}),
    Kind.RESPONSE_STYLE: frozenset({Action.PREFER, Action.AVOID}),
    Kind.COMFORT: frozenset({Action.LISTEN_BEFORE, Action.ASK_BEFORE, Action.PREFER}),
    Kind.TOPIC_BOUNDARY: frozenset({Action.AVOID, Action.ASK_BEFORE}),
    Kind.EXPRESSION_BOUNDARY: frozenset({Action.AVOID}),
    Kind.RELATIONSHIP_DEFINITION: frozenset({Action.PREFER, Action.DO_NOT_ASSUME}),
    Kind.DURABLE_BEHAVIOR: frozenset({Action.PREFER, Action.LISTEN_BEFORE, Action.ASK_BEFORE}),
    Kind.CUSTOM_PREFERENCE: frozenset({Action.HONOR_PREFERENCE}),
}

# kind별 허용 condition. 대부분은 always고, 조건부가 의미 있는 것만 넓힌다.
ALLOWED_CONDITIONS: dict[Kind, frozenset[Condition]] = {
    Kind.ADDRESS: frozenset({Condition.ALWAYS}),
    Kind.RESPONSE_STYLE: frozenset({Condition.ALWAYS, Condition.WHEN_ASKING_ADVICE}),
    Kind.COMFORT: frozenset({Condition.ALWAYS, Condition.WHEN_DISTRESSED}),
    Kind.TOPIC_BOUNDARY: frozenset({Condition.ALWAYS, Condition.WHEN_TOPIC_TAG}),
    Kind.EXPRESSION_BOUNDARY: frozenset({Condition.ALWAYS}),
    Kind.RELATIONSHIP_DEFINITION: frozenset({Condition.ALWAYS}),
    Kind.DURABLE_BEHAVIOR: frozenset(
        {Condition.ALWAYS, Condition.WHEN_DISTRESSED,
         Condition.WHEN_ASKING_ADVICE, Condition.CUSTOM_TRIGGER}
    ),
    Kind.CUSTOM_PREFERENCE: frozenset({Condition.ALWAYS, Condition.CUSTOM_TRIGGER}),
}


class ContractViolation(ValueError):
    """스키마·안전 계약 위반. 저장도 렌더도 하지 않는다."""


# ─────────────────────────────────────────────────────────────
# literal 위생 — 여기가 인젝션 방어선이다
# ─────────────────────────────────────────────────────────────

# bidi 재정렬 문자. 보이는 것과 다른 순서로 읽히게 만든다.
_BIDI = "‪‫‬‭‮⁦⁧⁨⁩‎‏"
# role/tool token으로 해석될 수 있는 표식.
_ROLE_TOKENS = re.compile(
    r"(?i)(?:^|\W)(system|assistant|developer|tool|user)\s*[:：]|"
    r"<\s*/?\s*(?:system|assistant|user|tool)|"
    r"\[/?INST\]|<\|.*?\|>|```"
)
# Markdown/XML delimiter — 인용 슬롯을 탈출해 구조를 만들 수 있는 문자.
_STRUCTURE = re.compile(r"[<>{}\[\]|`#*_~\\]")


def sanitize_literal(raw: str) -> str:
    """`target_literal` 위생 검사. 통과한 값만 렌더에 들어간다.

    통과시키는 게 목적이 아니라 **의심스러우면 거부하는 게** 목적이다. 거부되면 그 요구는
    literal 없이 tag로 표현하거나 custom_preference 승인 경로로 간다.
    """
    if raw is None:
        raise ContractViolation("literal이 None이다")
    text = unicodedata.normalize("NFKC", raw).strip()
    if not text:
        raise ContractViolation("빈 literal")
    if "\n" in text or "\r" in text:
        raise ContractViolation("literal은 단일행이어야 한다")
    if any(ch in text for ch in _BIDI):
        raise ContractViolation("bidi 제어문자가 들어 있다")
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in text):
        raise ContractViolation("제어문자가 들어 있다")
    if _STRUCTURE.search(text):
        raise ContractViolation("Markdown/XML delimiter가 들어 있다")
    if _ROLE_TOKENS.search(text):
        raise ContractViolation("role/tool token으로 읽힐 수 있다")
    if _grapheme_len(text) > MAX_LITERAL_GRAPHEMES:
        raise ContractViolation(
            f"literal이 {MAX_LITERAL_GRAPHEMES} grapheme을 넘는다"
        )
    return text


def _grapheme_len(text: str) -> int:
    """결합 문자를 하나로 세는 근사. 한글 조합·이모지 ZWJ 연쇄를 한 글자로 본다."""
    count = 0
    for ch in text:
        if unicodedata.combining(ch) or ch == "‍":
            continue
        count += 1
    return count


@dataclass(frozen=True, slots=True)
class Directive:
    kind: Kind
    action: Action
    condition: Condition
    polarity: Polarity
    target_tag: str | None = None
    target_literal: str | None = None


def validate(d: Directive) -> Directive:
    """조합·literal 검사를 모두 통과한 directive만 돌려준다."""
    if d.action not in ALLOWED_ACTIONS[d.kind]:
        raise ContractViolation(f"{d.kind.value}에 허용되지 않는 action: {d.action.value}")
    if d.condition not in ALLOWED_CONDITIONS[d.kind]:
        raise ContractViolation(
            f"{d.kind.value}에 허용되지 않는 condition: {d.condition.value}"
        )
    if d.condition is Condition.WHEN_TOPIC_TAG and not d.target_tag:
        raise ContractViolation("when_topic_tag는 target_tag가 필요하다")
    if d.target_literal is not None:
        sanitize_literal(d.target_literal)  # 위반이면 여기서 올라간다
    if d.target_tag is None and d.target_literal is None and d.kind not in (
        Kind.RESPONSE_STYLE, Kind.COMFORT
    ):
        raise ContractViolation(f"{d.kind.value}는 대상이 필요하다")
    return d


# ─────────────────────────────────────────────────────────────
# 렌더 — literal은 **인용된 데이터 슬롯**에만 들어간다
# ─────────────────────────────────────────────────────────────

_ACTION_KO: dict[Action, str] = {
    Action.USE: "이렇게 부른다",
    Action.AVOID: "피한다",
    Action.PREFER: "이 쪽을 택한다",
    Action.ASK_BEFORE: "먼저 물어본다",
    Action.LISTEN_BEFORE: "먼저 듣는다",
    Action.DO_NOT_ASSUME: "단정하지 않는다",
    Action.HONOR_PREFERENCE: "이 선호를 지킨다",
}

_CONDITION_KO: dict[Condition, str] = {
    Condition.ALWAYS: "늘",
    Condition.WHEN_DISTRESSED: "상대가 힘들어할 때",
    Condition.WHEN_ASKING_ADVICE: "상대가 조언을 구할 때",
    Condition.WHEN_TOPIC_TAG: "그 주제가 나올 때",
    Condition.CUSTOM_TRIGGER: "그 상황에서",
}


def render(d: Directive) -> str:
    """서버 고정 template. 사용자 값은 따옴표 안 **데이터**로만 들어간다.

    literal을 명령 위치(문장 앞·동사 자리)에 놓으면 그 자체가 지시로 읽힐 수 있다.
    항상 `「…」` 안에 넣어 인용된 대상임을 문법으로 고정한다.
    """
    validate(d)
    parts = [_CONDITION_KO[d.condition]]
    target = d.target_literal or d.target_tag
    if target:
        parts.append(f"「{target}」을(를)")
    parts.append(_ACTION_KO[d.action])
    if d.polarity is Polarity.NEGATIVE and d.action not in (Action.AVOID, Action.DO_NOT_ASSUME):
        parts.append("않는다")
    return "- " + " ".join(parts) + "."


def render_document(directives: list[Directive]) -> str:
    """published contract의 stable 블록. 항목이 없으면 빈 문자열이다.

    빈 블록을 넣으면 캐시 프리픽스만 늘고 의미가 없다.
    """
    lines = [render(d) for d in directives]
    if not lines:
        return ""
    return "[이 사람과의 약속]\n" + "\n".join(lines)
