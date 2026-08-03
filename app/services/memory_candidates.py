"""추출 후보 payload의 **versioned 스키마**(W8) — `memory-candidate-v1`.

LLM이 내놓는 것은 후보와 *제안*뿐이다. `proposed_action`/`proposed_target_fact_id`는 관측용이며
코드 판정(`memory_reconcile`)을 덮어쓰지 못한다.

extra field를 금지한 엄격 스키마다. 하나라도 어기면 **상태를 하나도 바꾸지 않고** 후보 전체를
schema 실패로 버린다(부분 수용 금지 — 반쯤 해석한 payload가 조용히 기억을 오염시킨다).

자연어 필드는 여기서 `naming.to_placeholder`를 이미 통과한다. 정규화·해시가 마스킹된 문자열 위에서
계산돼야 저장 표면과 hash가 갈라지지 않기 때문이다(살균은 normalizer가 이어받는다).
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services import memory_norm, memory_registry
from app.services.memory_norm import FactFields

SCHEMA_VERSION = "memory-candidate-v1"

# LLM이 제안할 수 있는 action(관측용). 코드 판정 결과의 이름과 같은 집합이다.
ACTION_ADD = "ADD"
ACTION_REINFORCE = "REINFORCE"
ACTION_SUPERSEDE = "SUPERSEDE"
ACTION_KEEP_BOTH = "KEEP_BOTH"
ACTION_IGNORE = "IGNORE"
ACTIONS: frozenset[str] = frozenset(
    {ACTION_ADD, ACTION_REINFORCE, ACTION_SUPERSEDE, ACTION_KEEP_BOTH, ACTION_IGNORE}
)

_REQUIRED = ("kind", "canonical_text", "importance", "confidence", "evidence_message_ids")
_OPTIONAL = (
    "subject", "predicate", "object_json", "event_time",
    "proposed_action", "proposed_target_fact_id",
)
_ALLOWED = frozenset(_REQUIRED + _OPTIONAL)
_TOP_ALLOWED = frozenset({"schema_version", "candidates"})

_INT64_MAX = 2**63 - 1


class CandidateSchemaError(ValueError):
    """후보 payload 스키마 위반 — 상태 변경 없이 job을 schema 실패로 끝낸다."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """검증을 통과한 후보 1건. 자연어 필드는 이미 placeholder 마스킹됐다."""

    kind: str
    canonical_text: str
    subject: str | None
    predicate: str | None
    object_json: Any | None
    event_time: datetime | None
    importance: float
    confidence: float
    evidence_message_ids: tuple[int, ...]
    proposed_action: str | None
    proposed_target_fact_id: uuid.UUID | None

    def fields(self) -> FactFields:
        """정규화 입력 형태. 기존 fact 행도 같은 형태로 만들어야 hash 비교가 성립한다."""
        return FactFields(
            kind=self.kind,
            canonical_text=self.canonical_text,
            subject=self.subject,
            predicate=self.predicate,
            object_json=self.object_json,
            event_time=self.event_time,
        )


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CandidateSchemaError(f"{field}는 수치여야 한다: {value!r}")
    out = float(value)
    if not math.isfinite(out):
        raise CandidateSchemaError(f"{field}가 유한하지 않다: {value!r}")
    return out


def _event_time(value: object) -> datetime | None:
    """offset이 **명시된** RFC 3339만 받는다. naive/날짜만/문자열 아님은 전부 거부."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise CandidateSchemaError(f"event_time은 RFC 3339 문자열이어야 한다: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as e:
        raise CandidateSchemaError(f"event_time이 RFC 3339가 아니다: {value!r}") from e
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CandidateSchemaError(f"event_time에 offset이 없다: {value!r}")
    return parsed


def _evidence_ids(value: object, allowed: frozenset[int]) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise CandidateSchemaError("evidence_message_ids는 비어 있지 않은 배열이어야 한다")
    out: list[int] = []
    for raw in value:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise CandidateSchemaError(f"evidence_message_ids 원소가 정수가 아니다: {raw!r}")
        if raw <= 0 or raw > _INT64_MAX:
            raise CandidateSchemaError(f"evidence_message_ids 원소가 bigint 범위 밖: {raw!r}")
        if raw in out:
            raise CandidateSchemaError(f"evidence_message_ids에 중복: {raw!r}")
        if raw not in allowed:
            # payload의 message_ids 밖 = 이 배치가 근거로 삼을 수 없는 메시지(타 유저/타 구간).
            raise CandidateSchemaError(f"payload message_ids 밖의 evidence id: {raw!r}")
        out.append(raw)
    return tuple(out)


def _fact_id(value: object) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as e:
        raise CandidateSchemaError(f"proposed_target_fact_id가 uuid가 아니다: {value!r}") from e


def _mask(value: Any, nickname: str | None) -> Any:
    """자연어 표면의 실명 스템을 토큰으로. 구현은 memory_norm에 한 벌만 둔다(저장 직전 강제와 공유)."""
    return memory_norm.mask_tree(value, nickname)


def _one(raw: object, allowed_ids: frozenset[int], nickname: str | None) -> Candidate:
    if not isinstance(raw, dict):
        raise CandidateSchemaError(f"후보는 객체여야 한다: {raw!r}")
    extra = set(raw) - _ALLOWED
    if extra:
        raise CandidateSchemaError(f"허용되지 않은 필드: {sorted(extra)}")
    missing = [k for k in _REQUIRED if k not in raw]
    if missing:
        raise CandidateSchemaError(f"필수 필드 누락: {missing}")

    kind = raw["kind"]
    if not memory_registry.is_kind(kind):
        raise CandidateSchemaError(f"registry 밖 kind: {kind!r}")

    canonical_text = raw["canonical_text"]
    if not isinstance(canonical_text, str) or not canonical_text.strip():
        raise CandidateSchemaError("canonical_text는 비어 있지 않은 문자열이어야 한다")

    subject = raw.get("subject")
    if subject is not None and not isinstance(subject, str):
        raise CandidateSchemaError(f"subject는 문자열이거나 null이다: {subject!r}")

    predicate = raw.get("predicate")
    object_json = raw.get("object_json")
    if (predicate is None) != (object_json is None):
        # 한쪽만 있으면 structured 신원이 성립하지 않는다(hash의 value가 무엇인지 정해지지 않음).
        raise CandidateSchemaError("predicate와 object_json은 둘 다 있거나 둘 다 없어야 한다")
    if predicate is not None and not memory_registry.is_predicate(predicate):
        raise CandidateSchemaError(f"registry 밖 predicate: {predicate!r}")

    confidence = _finite(raw["confidence"], "confidence")
    if not (0.0 <= confidence <= 1.0):  # DDL CHECK와 같은 범위
        raise CandidateSchemaError(f"confidence가 0~1 밖: {confidence!r}")
    importance = _finite(raw["importance"], "importance")

    proposed_action = raw.get("proposed_action")
    if proposed_action is not None and proposed_action not in ACTIONS:
        raise CandidateSchemaError(f"허용되지 않은 proposed_action: {proposed_action!r}")

    masked_object = _mask(object_json, nickname) if object_json is not None else None
    if masked_object is not None:
        try:  # 정규화·해시 전에 직렬화 가능성을 확인한다(JCS 불가 타입 조기 거부)
            memory_norm.jcs_dumps(masked_object)
        except ValueError as e:
            raise CandidateSchemaError(f"object_json 직렬화 불가: {e}") from e

    return Candidate(
        kind=kind,
        canonical_text=_mask(canonical_text, nickname),
        subject=_mask(subject, nickname) if subject is not None else None,
        predicate=predicate,
        object_json=masked_object,
        event_time=_event_time(raw.get("event_time")),
        importance=importance,
        confidence=confidence,
        evidence_message_ids=_evidence_ids(raw["evidence_message_ids"], allowed_ids),
        proposed_action=proposed_action,
        proposed_target_fact_id=_fact_id(raw.get("proposed_target_fact_id")),
    )


def parse_candidates(
    payload: object, *, allowed_message_ids: object, nickname: str | None = None
) -> list[Candidate]:
    """extractor payload → 검증된 후보 목록. 하나라도 어기면 `CandidateSchemaError`(전량 폐기).

    `allowed_message_ids`는 이 배치 payload의 `message_ids`다 — evidence는 반드시 이 안이어야 한다.
    """
    if not isinstance(payload, dict):
        raise CandidateSchemaError("payload는 객체여야 한다")
    extra = set(payload) - _TOP_ALLOWED
    if extra:
        raise CandidateSchemaError(f"허용되지 않은 최상위 필드: {sorted(extra)}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise CandidateSchemaError(f"알 수 없는 schema_version: {payload.get('schema_version')!r}")
    raw_list = payload.get("candidates")
    if not isinstance(raw_list, list):
        raise CandidateSchemaError("candidates는 배열이어야 한다")

    allowed = frozenset(allowed_message_ids)  # type: ignore[arg-type]
    return [_one(raw, allowed, nickname) for raw in raw_list]
