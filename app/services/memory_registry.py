"""정규화 기억(W8)의 **어휘 registry** — kind 5종 + predicate 13종의 cardinality.

이건 mem0 이관이 아니라 **신설**이다. 현행 mem0는 자유문 기억만 만들어 구조화 vocabulary가 아예
없었다. 여기 값이 곧 계약이라 추출 스키마 검증(`memory_candidates`)과 판정(`memory_reconcile`)이
같은 표를 본다.

확장 규칙(중요):
- **항목 추가**는 `normalization_version`을 올리지 않는다(정규화 산출물이 안 바뀐다).
  shadow 단계에서 실제 추출 분포를 보고 넓힌다.
- **기존 predicate의 cardinality 변경**은 판정 결과를 바꾸므로 새 normalization version을 발급한다
  (`memory_norm` 참조). 제자리 재해시는 금지다.
- registry에 없는 kind/predicate는 스키마 단계에서 거부한다(조용한 오타 흡수 금지).
"""
from __future__ import annotations

from types import MappingProxyType

# ─────────────────────────────────────────────────────────────
# kind — predicate가 없는 자유문 후보도 반드시 갖는다.
# ─────────────────────────────────────────────────────────────
KIND_PROFILE = "profile"
KIND_PREFERENCE = "preference"
KIND_RELATIONSHIP = "relationship"
KIND_EVENT = "event"
KIND_EMOTION = "emotion"

KINDS: frozenset[str] = frozenset(
    {KIND_PROFILE, KIND_PREFERENCE, KIND_RELATIONSHIP, KIND_EVENT, KIND_EMOTION}
)

# ─────────────────────────────────────────────────────────────
# predicate — cardinality가 SUPERSEDE(single)와 KEEP_BOTH(multi)를 가른다.
# ─────────────────────────────────────────────────────────────
SINGLE = "single"
MULTI = "multi"

PREDICATES: MappingProxyType[str, str] = MappingProxyType(
    {
        "residence": SINGLE,            # 사는 지역
        "occupation": SINGLE,           # 하는 일·학교
        "household": SINGLE,            # 함께 사는 형태
        "relationship_status": SINGLE,  # 연애·결혼 상태
        "current_focus": SINGLE,        # 지금 매달리는 일(시험·이직 등)
        "likes": MULTI,                 # 좋아하는 것
        "dislikes": MULTI,              # 싫어하는 것
        "interest": MULTI,              # 관심사·취미
        "person": MULTI,                # 주변 인물과의 관계
        "pet": MULTI,                   # 반려동물
        "habit": MULTI,                 # 반복하는 습관
        "concern": MULTI,               # 지속되는 고민
        "event": MULTI,                 # 있었던 일
    }
)


class UnknownVocabularyError(ValueError):
    """registry 밖의 kind/predicate — 스키마 실패로 다뤄 후보 전체를 버린다."""


def is_kind(value: object) -> bool:
    return isinstance(value, str) and value in KINDS


def is_predicate(value: object) -> bool:
    return isinstance(value, str) and value in PREDICATES


def cardinality(predicate: str) -> str:
    """`single` | `multi`. registry 밖이면 기본값으로 흡수하지 않고 raise한다."""
    try:
        return PREDICATES[predicate]
    except KeyError as e:
        raise UnknownVocabularyError(f"registry에 없는 predicate: {predicate!r}") from e


def is_single(predicate: str) -> bool:
    return cardinality(predicate) == SINGLE
