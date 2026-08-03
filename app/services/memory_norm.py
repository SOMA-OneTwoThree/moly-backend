"""정규화 + **공용 해시 함수**(W8). fact의 콘텐츠 식별자와 forget marker의 deny key를 한 곳에서 만든다.

이 모듈이 지키는 세 가지:

1. **해시 이중 신원 금지.** `memory_facts.(normalization_version, content_hash)`와
   `memory_forget_markers.(normalization_version, normalized_hash)`는 **같은 산출물**이다.
   계산 경로는 `content_hash()` 하나뿐이고, forget은 fact의 값을 그대로 복사한다(§W8).
   두 곳에 따로 구현하면 잊어달라고 한 사실이 되살아난다.
2. **제자리 재해시 금지.** 정규화 산출물이 달라지는 변경만 새 version을 발급하고, **이전
   normalizer는 registry에 영구 보관**한다. 기존 fact/marker의 hash를 새 규칙으로 다시 계산하지
   않는다. `register()`가 같은 version에 다른 구현을 덮어쓰는 것을 거부해 이를 코드로 못 박는다.
3. **미지원 version은 조용히 넘어가지 않는다.** hard filter는 그 유저의 marker가 가진 **모든**
   version의 normalizer로 후보를 다시 계산해야 하므로, 하나라도 지원 못 하면
   `UnsupportedNormalizationVersionError`로 job을 실패시킨다(무음 우회 = 프라이버시 회귀).

의존 방향: `memory`(façade) ← `memory_norm` ← `memory_repo`/`memory_reconcile`.
살균 규칙은 프롬프트 주입 방어와 같은 것을 써야 하므로 façade의 `sanitize_text`를 승계한다.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from app.services import memory_registry, naming
from app.services.memory import sanitize_text

# 최초 정규화 계약. "첫 계약"임이 이름에 드러나야 뒤에 나올 version과 혼동이 없다.
NORMALIZATION_VERSION_V1 = "memory-fact-v1"
CURRENT_NORMALIZATION_VERSION = NORMALIZATION_VERSION_V1

# 지금까지 **발급된 적 있는** 전 version. 여기서 항목을 지우는 것은 곧 그 version의 marker를
# 검증 불가로 만드는 것(= 잊은 사실 부활)이라 금지다. 새 version은 추가만 한다.
HISTORICAL_NORMALIZATION_VERSIONS: tuple[str, ...] = (NORMALIZATION_VERSION_V1,)


class UnsupportedNormalizationVersionError(Exception):
    """registry에 없는 normalization version — publish 금지 + 경보 대상(무음 우회 차단)."""


# ─────────────────────────────────────────────────────────────
# 정규화 입출력
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class FactFields:
    """정규화 입력. 추출 후보와 기존 fact 행이 **같은 형태**로 들어와야 hash가 비교 가능하다."""

    kind: str
    canonical_text: str
    subject: str | None = None
    predicate: str | None = None
    object_json: Any | None = None
    event_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class NormalizedFact:
    """정규화 결과 + 그 결과의 content hash. 저장되는 값도 여기 것을 쓴다(표면/해시 불일치 방지)."""

    version: str
    kind: str
    canonical_text: str
    subject: str | None
    predicate: str | None
    object_json: Any | None
    event_time: datetime | None
    content_hash: str

    @property
    def identity_key(self) -> tuple[str, str | None, str | None]:
        """정규화한 `(kind, subject, predicate)` — ADD/SUPERSEDE/KEEP_BOTH 비교의 identity."""
        return (self.kind, self.subject, self.predicate)

    @property
    def deny_key(self) -> tuple[str, str]:
        """forget marker와 비교하는 `(version, hash)`. marker 쪽 값과 **같은 산출물**이다."""
        return (self.version, self.content_hash)


# ─────────────────────────────────────────────────────────────
# RFC 8785 JCS — hash 입력 직렬화. 키 정렬은 UTF-16 code unit 순서다.
# ─────────────────────────────────────────────────────────────
def _jcs_number(value: int | float) -> str:
    """ES6 Number::toString 규칙. 정수 표현이 가능한 값은 소수점 없이 쓴다."""
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise ValueError(f"JCS 직렬화 불가(비유한 수): {value!r}")
    if value == int(value) and abs(value) < 1e21:
        return str(int(value))
    out = repr(value)
    if "e" in out:  # Python '1e-07' → ES6 '1e-7' (지수부 leading zero 제거)
        mant, exp = out.split("e")
        sign = "-" if exp[0] == "-" else "+"
        out = f"{mant}e{sign}{exp.lstrip('+-').lstrip('0') or '0'}"
    return out


def jcs_dumps(value: Any) -> str:
    """RFC 8785 canonical JSON. 이 문자열의 UTF-8 바이트가 hash 입력이다."""
    if value is None:
        return "null"
    if isinstance(value, bool):  # bool은 int의 하위형이라 반드시 먼저 본다
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _jcs_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # JSON 최소 이스케이프 = JCS 규칙
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(jcs_dumps(v) for v in value) + "]"
    if isinstance(value, dict):
        # UTF-16 code unit 순서 = utf-16-be 바이트열 사전순(파이썬 기본 code point 정렬과 다르다).
        items = sorted(value.items(), key=lambda kv: str(kv[0]).encode("utf-16-be"))
        return "{" + ",".join(f"{jcs_dumps(str(k))}:{jcs_dumps(v)}" for k, v in items) + "}"
    raise ValueError(f"JCS 직렬화 불가 타입: {type(value).__name__}")


# ─────────────────────────────────────────────────────────────
# normalizer v1
# ─────────────────────────────────────────────────────────────
_WS_RUN = re.compile(r"\s+")


class Normalizer(Protocol):
    version: str

    def normalize(self, fields: FactFields) -> NormalizedFact: ...


class _NormalizerV1:
    """`memory-fact-v1`.

    문자열: (placeholder 치환은 호출측이 이미 끝냈다는 전제) 살균 → NFKC → 연속 Unicode whitespace를
    ASCII 공백 하나로 → 앞뒤 trim. `object_json`의 문자열 **값**에 재귀 적용한 뒤 JCS로 직렬화한다.
    `event_time`은 UTC로 바꾸되 정밀도를 버리지 않는 RFC 3339로 쓴다.
    """

    version = NORMALIZATION_VERSION_V1

    @staticmethod
    def _text(value: str) -> str:
        # 살균을 정규화 안에서 한다 — 저장 표면(canonical_text)과 hash 입력이 같은 값이어야
        # 대괄호/제어문자만 다른 후보가 "다른 사실"로 갈라지지 않는다.
        cleaned = sanitize_text(value)
        return _WS_RUN.sub(" ", unicodedata.normalize("NFKC", cleaned)).strip()

    @classmethod
    def _json(cls, value: Any) -> Any:
        if isinstance(value, str):
            return cls._text(value)
        if isinstance(value, dict):  # 키는 JCS 정렬 대상이라 값만 정규화한다
            return {k: cls._json(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._json(v) for v in value]
        return value

    @staticmethod
    def _event_time(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:  # offset 없는 시각은 스키마 단계에서 이미 거부된다(이중 방어)
            raise ValueError("event_time에 offset이 없다(RFC 3339 위반)")
        return value.astimezone(timezone.utc)

    def normalize(self, fields: FactFields) -> NormalizedFact:
        if not memory_registry.is_kind(fields.kind):
            raise memory_registry.UnknownVocabularyError(f"registry 밖 kind: {fields.kind!r}")
        predicate = fields.predicate
        if predicate is not None and not memory_registry.is_predicate(predicate):
            raise memory_registry.UnknownVocabularyError(f"registry 밖 predicate: {predicate!r}")

        canonical_text = self._text(fields.canonical_text)
        subject = self._text(fields.subject) if fields.subject is not None else None
        subject = subject or None  # 정규화 후 빈 문자열은 None과 같은 신원으로 접는다
        object_json = self._json(fields.object_json) if predicate is not None else None
        event_time = self._event_time(fields.event_time)

        # structured 후보는 object_json, predicate 없는 후보는 정규화한 canonical_text가 value다.
        value: Any = object_json if predicate is not None else canonical_text
        payload = {
            "v": self.version,
            "kind": fields.kind,
            "subject": subject,
            "predicate": predicate,
            "value": value,
            "event_time": _rfc3339(event_time),
        }
        digest = hashlib.sha256(jcs_dumps(payload).encode("utf-8")).hexdigest()
        return NormalizedFact(
            version=self.version,
            kind=fields.kind,
            canonical_text=canonical_text,
            subject=subject,
            predicate=predicate,
            object_json=object_json,
            event_time=event_time,
            content_hash=digest,
        )


def _rfc3339(value: datetime | None) -> str | None:
    """UTC RFC 3339. 마이크로초가 있으면 그대로 남긴다(정밀도 손실 금지)."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ─────────────────────────────────────────────────────────────
# version registry — 추가만, 삭제·덮어쓰기 금지.
# ─────────────────────────────────────────────────────────────
_NORMALIZERS: dict[str, Normalizer] = {}


def register(normalizer: Normalizer) -> None:
    """새 version 등록. 같은 version에 **다른 구현**을 얹는 것은 제자리 재해시라 거부한다."""
    existing = _NORMALIZERS.get(normalizer.version)
    if existing is not None and existing is not normalizer:
        raise ValueError(
            f"이미 등록된 normalization version을 덮어쓸 수 없다: {normalizer.version!r} "
            "(정규화 산출물이 바뀌면 새 version을 발급하고 이전 normalizer는 그대로 둔다)"
        )
    _NORMALIZERS[normalizer.version] = normalizer


register(_NormalizerV1())


def supported_versions() -> frozenset[str]:
    return frozenset(_NORMALIZERS)


def get_normalizer(version: str) -> Normalizer:
    try:
        return _NORMALIZERS[version]
    except KeyError as e:
        raise UnsupportedNormalizationVersionError(
            f"지원하지 않는 normalization version: {version!r}"
        ) from e


def normalize(fields: FactFields, *, version: str = CURRENT_NORMALIZATION_VERSION) -> NormalizedFact:
    """`version`의 normalizer로 정규화. 미등록 version은 raise(무음 폴백 금지)."""
    return get_normalizer(version).normalize(fields)


def content_hash(fields: FactFields, *, version: str = CURRENT_NORMALIZATION_VERSION) -> str:
    """**공용 해시 함수.** fact의 `content_hash`도 marker의 `normalized_hash`도 여기서만 나온다."""
    return normalize(fields, version=version).content_hash


def hashes_for_versions(fields: FactFields, versions: set[str]) -> dict[str, str]:
    """version별 hash를 한 번에. hard filter가 marker/fact의 **모든** version으로 재계산할 때 쓴다.

    지원 못 하는 version이 하나라도 있으면 부분 결과를 돌려주지 않고 raise한다 — 반쯤 계산된
    hard filter는 잊은 사실을 통과시킨다.
    """
    return {v: content_hash(fields, version=v) for v in sorted(versions)}


def mask_for_storage(value: str | None, nickname: str | None) -> str | None:
    """자연어 컬럼 저장 직전 강제 — 실명 스템을 `{유저이름}` 토큰으로. 자연 멱등(재적용 무해)."""
    return naming.to_placeholder(value, nickname)


def mask_tree(value: Any, nickname: str | None) -> Any:
    """dict/list 안의 문자열까지 재귀로 마스킹. jsonb 컬럼(object_json)처럼 중첩 구조용.

    후보 파싱과 저장 직전 강제가 **같은 구현을 써야** 한 쪽만 고쳐져 실명이 새는 일이 없다
    (해시를 공용 함수 하나로 두는 것과 같은 이유).
    """
    if isinstance(value, str):
        return mask_for_storage(value, nickname)
    if isinstance(value, dict):
        return {k: mask_tree(v, nickname) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_tree(v, nickname) for v in value]
    return value
