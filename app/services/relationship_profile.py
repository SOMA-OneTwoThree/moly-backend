"""관계 프로필(W9)의 **문서·렌더러** — 순수 함수만. SQL·트랜잭션은 `relationship_profile_repo`.

지금 프롬프트에 들어가는 기억은 `chat_contexts.memory_text`(mem0 최근 20개를 자유문으로 나열)다.
그걸 **칸이 고정된** 투영으로 바꾸는 것이 W9다(실제 교체는 W10 cutover).

이 모듈이 지키는 것:

1. **칸 상한.** stance / known_facts(≤5) / recent_threads(≤3) / inferred_tendencies(≤2).
   자유 문장 한 덩어리를 만들지 않는다 — 칸이 없으면 모델이 기억을 나열하듯 읊는다.
2. **≤400토큰.** 안정 프리픽스에 실리므로 예산을 넘으면 렌더가 실패하는 대신 **결정적 순서로**
   덜 중요한 칸부터 덜어낸다(job 실패로 프로필이 통째로 비는 것보다 낫다).
3. **결정적 `render_hash`.** 같은 입력 → 같은 해시. 해시가 같으면 새 version을 만들지 않는다
   (재발행 = 안정 프리픽스 교체 = 캐시 파괴 = 비용 폭증).
4. **항목마다 근거.** 모든 항목은 불변 `item_key`와 최소 1개의 `source_refs`를 갖는다. 근거 없는
   문장은 어디서 왔는지 되짚을 수 없어 렌더 시점 재검증(무효 근거 항목 제외)이 불가능하다.
   근거를 댈 수 없는 stance는 넣지 말고 비운다.
5. **실명 금지·살균.** 저장 문자열은 `naming.to_placeholder`로 마스킹하고 `memory.sanitize_text`로
   제어문자·대괄호를 걷어낸다(가짜 섹션 헤더 위조 = 프롬프트 인젝션 통로).
6. **3언어.** 라벨은 `i18n.pick`으로 고른다. `language == "ko"` 하드코딩 금지(SOMA-344).

`render_hash`는 W8 `memory_norm.content_hash`를 **재사용하지 않는다**. 그 해시는 fact의 콘텐츠
식별자이자 forget marker의 deny key라는 단일 신원을 갖고(`memory_norm` docstring), 프로필 렌더
결과가 그 이름공간에 섞이면 marker 비교가 프로필 해시와 우연히 만나는 경로가 생긴다. 대신 직렬화
규약(RFC 8785 JCS)만 `memory_norm.jcs_dumps`로 공유해 "같은 입력 → 같은 바이트"를 한 곳에서 보증한다.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, replace

from app.services import i18n, memory_norm, naming
from app.services.agent.runtime import estimate_tokens
from app.services.memory import sanitize_text

# 문서/렌더 계약 버전. 문서 구조가 바뀌면 SCHEMA를, 렌더 문자열 산출이 바뀌면 RENDER를 올린다
# (둘 다 render_hash 입력이라 올리는 순간 전 유저가 한 번 재발행된다 — 가볍게 올리지 않는다).
DOCUMENT_SCHEMA_VERSION = "relationship-profile-v1"
RENDER_VERSION = "relationship-profile-render-v1"

SLOT_STANCE = "stance"
SLOT_KNOWN_FACTS = "known_facts"
SLOT_RECENT_THREADS = "recent_threads"
SLOT_INFERRED = "inferred_tendencies"

# 칸 상한(명세 §W9). stance는 한 항목이거나 없다.
MAX_KNOWN_FACTS = 5
MAX_RECENT_THREADS = 3
MAX_INFERRED = 2

# 렌더 결과 상한. 토크나이저 없는 **근사**치를 쓴다(도구 결과 예산과 같은 함수 — 예산 계산 방식이
# 두 벌이 되면 한쪽만 보정됐을 때 조용히 어긋난다).
RENDER_TOKEN_BUDGET = 400

SOURCE_FACT = "fact"
SOURCE_INSIGHT = "insight"
SOURCE_TYPES = frozenset((SOURCE_FACT, SOURCE_INSIGHT))


class ProfileError(Exception):
    """프로필 문서 계약 위반 — 렌더/저장 어느 쪽도 진행하지 않는다."""


class DocumentSchemaError(ProfileError):
    """문서 구조 위반(빈 item_key·중복 key·근거 없음·미지원 source type 등)."""


class SlotOverflowError(ProfileError):
    """칸 상한 초과 — 저장된 문서가 계약을 어긴 상태라 조용히 잘라 렌더하지 않는다."""


# ─────────────────────────────────────────────────────────────
# 문서 구조
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class SourceRef:
    """항목의 근거 1건. `type`은 fact | insight, `id`는 그 행의 uuid(문자열로 정규화)."""

    type: str
    id: str

    def __post_init__(self) -> None:
        if self.type not in SOURCE_TYPES:
            raise DocumentSchemaError(f"지원하지 않는 source type: {self.type!r}")
        object.__setattr__(self, "id", str(self.id))

    def as_json(self) -> dict:
        return {"type": self.type, "id": self.id}

    @property
    def key(self) -> tuple[str, str]:
        return (self.type, self.id)


@dataclass(frozen=True, slots=True)
class ProfileItem:
    """칸 안의 항목 1개. `item_key`는 문서 안에서 유일하고 재생성 사이에 불변이어야 한다."""

    item_key: str
    text: str
    source_refs: tuple[SourceRef, ...]

    def __post_init__(self) -> None:
        if not self.item_key.strip():
            raise DocumentSchemaError("item_key가 비었다")
        if not self.text.strip():
            raise DocumentSchemaError(f"항목 본문이 비었다: item_key={self.item_key!r}")
        if not self.source_refs:
            # 근거 없는 항목은 렌더 시점에 유효성을 되짚을 수 없다(잊은 내용이 남는 경로).
            raise DocumentSchemaError(f"근거 없는 항목은 만들 수 없다: item_key={self.item_key!r}")
        # 근거는 중복 제거 후 (type, id)로 정렬 — 같은 내용이 순서 차이로 다른 해시가 되지 않게.
        uniq = {r.key: r for r in self.source_refs}
        object.__setattr__(self, "source_refs", tuple(uniq[k] for k in sorted(uniq)))

    def as_json(self) -> dict:
        return {
            "item_key": self.item_key,
            "text": self.text,
            "source_refs": [r.as_json() for r in self.source_refs],
        }


@dataclass(frozen=True, slots=True)
class ProfileDocument:
    """칸 고정 문서. 상한 위반은 생성 시점에 raise한다(조용한 절삭 금지)."""

    stance: ProfileItem | None = None
    known_facts: tuple[ProfileItem, ...] = ()
    recent_threads: tuple[ProfileItem, ...] = ()
    inferred_tendencies: tuple[ProfileItem, ...] = ()

    def __post_init__(self) -> None:
        for slot, items, cap in (
            (SLOT_KNOWN_FACTS, self.known_facts, MAX_KNOWN_FACTS),
            (SLOT_RECENT_THREADS, self.recent_threads, MAX_RECENT_THREADS),
            (SLOT_INFERRED, self.inferred_tendencies, MAX_INFERRED),
        ):
            if len(items) > cap:
                raise SlotOverflowError(f"{slot} 칸 상한 초과: {len(items)} > {cap}")
        keys = [i.item_key for i in self.items]
        if len(set(keys)) != len(keys):
            # edge 행은 (profile, item_key, 근거)로만 식별된다 — key가 겹치면 근거가 뒤섞인다.
            raise DocumentSchemaError(f"item_key가 문서 안에서 중복됐다: {sorted(keys)}")

    @property
    def items(self) -> tuple[ProfileItem, ...]:
        """칸 순서대로 편 전 항목."""
        return (
            *((self.stance,) if self.stance is not None else ()),
            *self.known_facts,
            *self.recent_threads,
            *self.inferred_tendencies,
        )

    @property
    def is_empty(self) -> bool:
        return not self.items

    def edges(self) -> frozenset[tuple[str, str, str]]:
        """`(item_key, source_type, source_id)` 집합 — edge 행과 양방향 대조에 쓴다."""
        return frozenset(
            (item.item_key, ref.type, ref.id) for item in self.items for ref in item.source_refs
        )

    def as_json(self) -> dict:
        return {
            "schema_version": DOCUMENT_SCHEMA_VERSION,
            SLOT_STANCE: self.stance.as_json() if self.stance is not None else None,
            SLOT_KNOWN_FACTS: [i.as_json() for i in self.known_facts],
            SLOT_RECENT_THREADS: [i.as_json() for i in self.recent_threads],
            SLOT_INFERRED: [i.as_json() for i in self.inferred_tendencies],
        }


def document_from_json(data: object) -> ProfileDocument:
    """저장된 `document_json` → 문서. 계약 위반은 흡수하지 않고 raise한다."""
    if not isinstance(data, dict):
        raise DocumentSchemaError(f"document_json이 객체가 아니다: {type(data).__name__}")
    if data.get("schema_version") != DOCUMENT_SCHEMA_VERSION:
        raise DocumentSchemaError(f"지원하지 않는 문서 버전: {data.get('schema_version')!r}")
    stance_raw = data.get(SLOT_STANCE)
    return ProfileDocument(
        stance=_item_from_json(stance_raw) if stance_raw is not None else None,
        known_facts=_items_from_json(data.get(SLOT_KNOWN_FACTS)),
        recent_threads=_items_from_json(data.get(SLOT_RECENT_THREADS)),
        inferred_tendencies=_items_from_json(data.get(SLOT_INFERRED)),
    )


def _items_from_json(raw: object) -> tuple[ProfileItem, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DocumentSchemaError(f"칸이 배열이 아니다: {type(raw).__name__}")
    return tuple(_item_from_json(r) for r in raw)


def _item_from_json(raw: object) -> ProfileItem:
    if not isinstance(raw, dict):
        raise DocumentSchemaError(f"항목이 객체가 아니다: {type(raw).__name__}")
    refs = raw.get("source_refs")
    if not isinstance(refs, list):
        raise DocumentSchemaError("source_refs가 배열이 아니다")
    parsed = []
    for r in refs:
        if not isinstance(r, dict) or "type" not in r or "id" not in r:
            raise DocumentSchemaError(f"source_ref 형식 위반: {r!r}")
        parsed.append(SourceRef(type=str(r["type"]), id=str(r["id"])))
    return ProfileItem(
        item_key=str(raw.get("item_key") or ""),
        text=str(raw.get("text") or ""),
        source_refs=tuple(parsed),
    )


# ─────────────────────────────────────────────────────────────
# 살균·마스킹 — 저장되는 문자열은 전부 이 문을 통과한다.
# ─────────────────────────────────────────────────────────────
_WS_RUN = re.compile(r"\s+")


def clean_text(value: str, nickname: str | None) -> str:
    """실명 마스킹 → 살균 → 공백 정규화. 자연 멱등(이미 마스킹된 문자열에 재적용해도 무해)."""
    masked = naming.to_placeholder(value, nickname) or ""
    return _WS_RUN.sub(" ", sanitize_text(masked)).strip()


def build_item(
    *, item_key: str, text: str, source_refs: object, nickname: str | None = None
) -> ProfileItem:
    """생성기(LLM/집계) 결과 → 저장 가능한 항목. 근거는 `SourceRef` 또는 `{"type","id"}`."""
    refs = tuple(
        r if isinstance(r, SourceRef) else SourceRef(type=str(r["type"]), id=str(r["id"]))
        for r in source_refs  # type: ignore[union-attr]
    )
    return ProfileItem(item_key=item_key.strip(), text=clean_text(text, nickname), source_refs=refs)


def build_document(
    *,
    stance: ProfileItem | None = None,
    known_facts: object = (),
    recent_threads: object = (),
    inferred_tendencies: object = (),
) -> ProfileDocument:
    """칸 상한까지만 **앞에서부터** 채운 문서. 생성기가 넘치게 뽑아도 프롬프트가 부풀지 않는다.

    상한 초과를 여기서는 절삭한다(생성기는 우선순위 순으로 준다는 계약). 반대로 이미 **저장된**
    문서가 상한을 넘으면 `document_from_json`이 raise한다 — 그건 계약 위반이지 절삭 대상이 아니다.
    """
    return ProfileDocument(
        stance=stance,
        known_facts=tuple(known_facts)[:MAX_KNOWN_FACTS],
        recent_threads=tuple(recent_threads)[:MAX_RECENT_THREADS],
        inferred_tendencies=tuple(inferred_tendencies)[:MAX_INFERRED],
    )


# ─────────────────────────────────────────────────────────────
# 렌더 — 라벨은 3언어. 문자열은 placeholder 그대로 저장하고, 현재 이름 치환은 프롬프트 주입
# 직전에 `naming.render`가 한다(개명하면 과거 프로필도 새 이름으로 보인다).
# ─────────────────────────────────────────────────────────────
_LABELS: dict[str, dict[str, str]] = {
    SLOT_STANCE: {"ko": "사이", "en": "Where you two stand", "ja": "いまの間柄"},
    SLOT_KNOWN_FACTS: {"ko": "알고 있는 것", "en": "What you know", "ja": "知っていること"},
    SLOT_RECENT_THREADS: {"ko": "요즘 이야기", "en": "Recent threads", "ja": "最近の話"},
    # 신뢰도 낮음 표시 — 짐작을 사실처럼 단정하지 않게 라벨에 못 박는다.
    SLOT_INFERRED: {
        "ko": "짐작 (확실하지 않음)",
        "en": "Guesses (not certain)",
        "ja": "推測 (確かではない)",
    },
}


def label(slot: str, language: str | None) -> str:
    return i18n.pick(_LABELS[slot], language)


@dataclass(frozen=True, slots=True)
class RenderResult:
    """렌더 산출물. `document`는 **예산 절삭 후** 실제로 렌더된 문서다.

    저장은 반드시 이 `document`를 쓴다 — 원본을 저장하면 document_json·edge가 렌더 문자열보다
    많은 항목을 가리켜 publish 시 양방향 대조가 깨진다.
    """

    document: ProfileDocument
    text: str
    render_hash: str
    tokens: int
    dropped_item_keys: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.text


def render(document: ProfileDocument, language: str | None) -> RenderResult:
    """문서 → 프롬프트 문자열(≤400토큰) + 결정적 해시.

    예산을 넘으면 **덜 중요한 칸부터 뒤에서** 항목을 덜어낸다(짐작 → 요즘 이야기 → 알고 있는 것).
    stance는 마지막까지 남기되 그것만으로도 넘치면 본문을 잘라 예산 안에 넣는다. 절삭은 입력이
    같으면 항상 같은 결과를 낸다(해시 안정성).
    """
    locale = i18n.resolve(language)
    doc = document
    dropped: list[str] = []
    text = _render_text(doc, locale)
    while estimate_tokens(text) > RENDER_TOKEN_BUDGET:
        doc, key = _drop_last(doc)
        if key is None:  # 더 덜어낼 항목이 없다 → stance 본문을 잘라 맞춘다
            doc = _shrink_stance(doc, locale)
            text = _render_text(doc, locale)
            break
        dropped.append(key)
        text = _render_text(doc, locale)
    return RenderResult(
        document=doc,
        text=text,
        render_hash=render_hash(doc, locale, text),
        tokens=estimate_tokens(text),
        dropped_item_keys=tuple(dropped),
    )


def _render_text(document: ProfileDocument, locale: str) -> str:
    blocks: list[str] = []
    if document.stance is not None:
        blocks.append(f"[{label(SLOT_STANCE, locale)}]\n{document.stance.text}")
    for slot, items in (
        (SLOT_KNOWN_FACTS, document.known_facts),
        (SLOT_RECENT_THREADS, document.recent_threads),
        (SLOT_INFERRED, document.inferred_tendencies),
    ):
        if items:
            lines = "\n".join(f"- {i.text}" for i in items)
            blocks.append(f"[{label(slot, locale)}]\n{lines}")
    return "\n".join(blocks)


def _drop_last(document: ProfileDocument) -> tuple[ProfileDocument, str | None]:
    """예산 초과 시 덜어낼 항목 하나 — 짐작 → 요즘 이야기 → 알고 있는 것 순의 마지막 항목."""
    for slot in (SLOT_INFERRED, SLOT_RECENT_THREADS, SLOT_KNOWN_FACTS):
        items: tuple[ProfileItem, ...] = getattr(document, slot)
        if items:
            return replace(document, **{slot: items[:-1]}), items[-1].item_key
    return document, None


def _shrink_stance(document: ProfileDocument, locale: str) -> ProfileDocument:
    """stance만 남았는데도 예산을 넘는 경우 — 본문을 문자 단위로 잘라 넣는다(빈 프로필 방지)."""
    stance = document.stance
    if stance is None:
        return document
    text = stance.text
    while text and estimate_tokens(_render_text(replace(document, stance=replace(stance, text=text)), locale)) > RENDER_TOKEN_BUDGET:
        text = text[: max(1, len(text) - 32)] if len(text) > 32 else ""
    if not text:
        return replace(document, stance=None)
    return replace(document, stance=replace(stance, text=text))


def render_hash(document: ProfileDocument, locale: str, text: str) -> str:
    """결정적 해시 — 같은 (문서, locale, 렌더 문자열)이면 항상 같다.

    근거(`source_refs`)까지 입력에 넣는다. 문장이 같아도 근거가 갈렸다면 그건 다른 프로필이고,
    publish 때 쓰는 edge 집합도 달라지기 때문이다(같은 해시로 접으면 edge가 옛 근거에 머문다).
    직렬화는 W8과 같은 RFC 8785 JCS를 쓰되 해시 이름공간은 분리한다(모듈 docstring 참조).
    """
    payload = {
        "v": RENDER_VERSION,
        "schema": DOCUMENT_SCHEMA_VERSION,
        "locale": locale,
        "document": document.as_json(),
        "rendered": text,
    }
    return hashlib.sha256(memory_norm.jcs_dumps(payload).encode("utf-8")).hexdigest()


def filter_by_valid_sources(
    document: ProfileDocument,
    *,
    valid_fact_ids: frozenset[str],
    valid_insight_ids: frozenset[str],
) -> ProfileDocument:
    """근거가 **하나라도** 무효인 항목을 통째로 뺀 문서.

    렌더 시점 재검증용이다(명세 §W9). 재생성이 밀려 있는 동안에도 forget/supersede된 근거가
    프롬프트에 남지 않게 한다. 부분 근거로 항목을 살리지 않는다 — 남은 근거가 그 문장을 지탱하는지
    코드가 판단할 수 없다.
    """

    def _ok(item: ProfileItem) -> bool:
        return all(
            (r.id in valid_fact_ids) if r.type == SOURCE_FACT else (r.id in valid_insight_ids)
            for r in item.source_refs
        )

    return ProfileDocument(
        stance=document.stance if document.stance is not None and _ok(document.stance) else None,
        known_facts=tuple(i for i in document.known_facts if _ok(i)),
        recent_threads=tuple(i for i in document.recent_threads if _ok(i)),
        inferred_tendencies=tuple(i for i in document.inferred_tendencies if _ok(i)),
    )


def mask_document(document: ProfileDocument, nickname: str | None) -> ProfileDocument:
    """저장 직전 강제 — 전 항목 본문에 마스킹·살균을 한 번 더 태운다(자연 멱등).

    생성기가 이미 통과시켰더라도 저장 경로가 자기 방어선을 갖는다(W8 `memory_repo`와 같은 규칙).
    """

    def _m(item: ProfileItem) -> ProfileItem:
        return replace(item, text=clean_text(item.text, nickname))

    return ProfileDocument(
        stance=_m(document.stance) if document.stance is not None else None,
        known_facts=tuple(_m(i) for i in document.known_facts),
        recent_threads=tuple(_m(i) for i in document.recent_threads),
        inferred_tendencies=tuple(_m(i) for i in document.inferred_tendencies),
    )


def source_ids(document: ProfileDocument) -> tuple[frozenset[str], frozenset[str]]:
    """문서가 참조하는 `(fact id 집합, insight id 집합)`."""
    facts = {r.id for i in document.items for r in i.source_refs if r.type == SOURCE_FACT}
    insights = {r.id for i in document.items for r in i.source_refs if r.type == SOURCE_INSIGHT}
    return frozenset(facts), frozenset(insights)


def as_uuid(value: str) -> uuid.UUID:
    """근거 id 문자열 → uuid. 형식이 아니면 raise(문자열 그대로 SQL에 넘기지 않는다)."""
    try:
        return uuid.UUID(str(value))
    except ValueError as e:
        raise DocumentSchemaError(f"근거 id가 uuid가 아니다: {value!r}") from e
