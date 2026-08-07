"""mem0 후보 추출 — 고정 스키마 파서와 **근거 span 원문 대조**.

기억 재설계(docs/ARCHITECTURE-capi.md 5.2절).

모델은 후보와 그 근거 span만 낸다. **span은 반드시 원문과 대조 검증한다** — 검증하지 않으면
모델이 지어낸 근거로 기억이 만들어지고, 나중에 "어디서 그런 말을 들었냐"에 답할 수 없다.

거부 정책(부분 수용 금지):
 · 스키마 위반 → 후보 **전량** 폐기. 일부만 받으면 어떤 게 검증된 건지 알 수 없다
 · 존재하지 않는 message id 참조 → 전량 폐기
 · span이 원문 밖이거나 hash 불일치 → 그 후보만 폐기(다른 후보는 유효할 수 있다)

추출 모델은 alias가 아닌 snapshot으로 고정한다(11.3절) — alias가 바뀌면 같은 prompt version에
다른 모델이 조용히 섞인다.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from app.services import i18n, memory
from app.services.mem0_ingest import Candidate, EvidenceSpan

_log = logging.getLogger("moly-worker")

# alias가 아닌 snapshot. 변경은 shadow eval + price catalog version 변경을 거친다.
EXTRACTOR_MODEL = "gpt-4.1-mini-2025-04-14"
# v2: 잊어달라 요청 제외 · 의도/완료 구분 · 3인칭 고정(2026-08-06 감사 지적).
# ⚠️ 프롬프트 규칙을 바꾸면 반드시 올린다 — 옛 규칙으로 뽑힌 기억과 구분이 안 되면
#    무엇을 재처리해야 하는지 알 수 없다.
EXTRACTOR_VERSION = "mem0-extractor-v2"
# 출력이 여기서 잘리면 JSON이 안 닫혀 **후보 전량 폐기**다(부분 수용 금지). 그러면 같은 입력으로
# 8번 재시도하고 전부 잘려 잡이 죽는다 — 그 사용자의 기억 사슬이 멈춘다(검토 지적 H-3).
#
# ⚠️ 이 값은 **반드시 `EXTRACTOR_MODEL`로 실측해서** 정한다. 대화 모델(GPT-5 계열)로 재면
#    추론 토큰이 섞여 몇 배로 나온다 — 그 값으로 정하면 필요 없이 크게 잡는다.
#
# 운영 대화 실측(2026-08-08, gpt-4.1-mini):
#   1턴 평균 45 / 최대 90 · 5턴 282 / 553 · 10턴 428 / 642 · **20턴 734 / 1,048**
# 턴당 약 37토큰으로 선형이라 폭주하지 않는다. 덩어리 상한 20턴 기준 1,048에 여유를 둔다.
# 덩어리를 키우면 이 값도 같이 재야 한다.
MAX_OUTPUT_TOKENS = 2_000
# 프롬프트에 적는 후보 개수 상한. 코드 쪽 상한(`mem0_ingest.MAX_CANDIDATES_PER_CHUNK`)과 같은
# 값이어야 한다 — 모델이 더 내면 뒷부분이 조용히 잘린다.
MAX_CANDIDATES = 24

# 허용 category — registry 밖 값은 조용히 흡수하지 않고 거부한다.
CATEGORIES: frozenset[str] = frozenset(
    {"preference", "emotion", "relationship", "event", "concern", "routine_intent"}
)


class ExtractionSchemaError(ValueError):
    """모델 출력이 계약을 어겼다 — 후보 전량 폐기."""


@dataclass(frozen=True, slots=True)
class SourceMessage:
    id: int
    sender: str
    content: str

    @property
    def utf8(self) -> bytes:
        return (self.content or "").encode("utf-8")

    def hash_of(self, start: int, end: int) -> str:
        return hashlib.sha256(self.utf8[start:end]).hexdigest()


_INSTRUCTIONS = {
    "ko": "- 사실은 한국어로 짧게 쓴다.",
    "en": "- Write each fact in short English.",
    "ja": "- 事実は短い日本語で書く。",
}


def build_system(language: str | None) -> str:
    """추출기 system 프롬프트. 캐피 페르소나와 무관한 별도 유틸리티 프롬프트다."""
    return (
        "너는 대화에서 오래 기억할 만한 사실 후보만 뽑는 추출기다. "
        "대화에 답하지 말고 JSON 객체 하나만 출력한다.\n\n"
        "[출력]\n"
        '{"candidates":[{"text":"...","category":"...","evidence":'
        '[{"message_id":정수,"start":정수,"end":정수}]}]}\n\n'
        f"[category] {' | '.join(sorted(CATEGORIES))}\n\n"
        "[규칙]\n"
        "- evidence는 유저 발화의 UTF-8 바이트 구간이다. 그 구간이 실제로 그 사실의 근거여야 한다.\n"
        "- 근거를 못 대면 후보로 만들지 않는다. 지어내지 않는다.\n"
        "- 상대(assistant) 발화는 대명사 해석에만 쓰고 단독 근거로 삼지 않는다.\n"
        "- 이번 대화에서 새로 드러난 사실만 뽑는다. 추측·요약은 후보가 아니다.\n"
        "- 정정과 부정도 새 사실이다. 유저가 앞서 말한 걸 뒤집으면('안 했어', '그거 아니야') "
        "그 부정을 후보로 뽑는다. 안 뽑으면 옛 사실이 영원히 남는다.\n"
        "- 사람 이름·호칭은 쓰지 않는다. {유저이름} 같은 토큰이 보이면 그대로 둔다.\n"
        "- 지금 착용 중인 것, 잔액, 오늘 루틴 완료 같은 현재 상태는 뽑지 않는다. "
        "정정이나 부정이어도 마찬가지다. 옷·소지품·루틴 목록은 서버에 원본이 있어서, "
        "기억으로 또 들고 있으면 원본과 어긋난 옛 내용이 대화를 이긴다.\n"
        # ↓ 아래 셋은 dev 실측에서 실제로 틀린 기억을 만든 경우들이다.
        "- 잊어달라·없던 걸로 해달라는 요청은 후보가 아니다. 지워달라는 말을 기억으로 "
        "만들면 그 내용을 오히려 영원히 들고 있게 된다.\n"
        "- 하려는 것과 한 것을 구분한다. '라면 먹을래'는 의도지 완료가 아니다. 아직 안 한 "
        "일을 한 것으로 쓰지 않는다. 의도면 '~하려고 한다'로 쓴다.\n"
        "- 항상 3인칭으로 쓴다. 유저 발화를 그대로 복사하지 말고 '유저가 ~한다'로 바꾼다. "
        "'그림 그리고 있어'처럼 주어가 없으면 누가 하는 일인지 알 수 없어 상대가 자기 일로 "
        "착각한다. '내'가 누구인지 모호한 문장을 만들지 않는다.\n"
        f"{_INSTRUCTIONS[i18n.resolve(language)]}\n"
        "- 뽑을 게 없으면 candidates를 빈 배열로 둔다. 억지로 만들지 않는다.\n"
        # 개수 지시가 없으면 모델이 몇 개를 낼지 알 수 없다. 대화 여러 턴이 한 번에 들어오므로
        # 상한을 명시해 출력이 잘리는 것을 막는다 — 잘리면 후보가 전량 폐기된다.
        f"- 후보는 최대 {MAX_CANDIDATES}개까지만 낸다. 넘치면 오래 기억할 값이 큰 것부터 고른다.\n"
        "- 설명도 코드펜스도 붙이지 말고 JSON만 출력한다."
    )


def render_conversation(messages: list[SourceMessage]) -> str:
    """`#id [역할] 본문`. 본문은 살균해 넣는다 — 유저가 이 프레이밍을 흉내내 근거를 위조하지 못하게."""
    return "\n".join(
        f"#{m.id} [{'상대' if m.sender != 'user' else '유저'}] {memory.sanitize_text(m.content or '')}"
        for m in sorted(messages, key=lambda x: x.id)
    )


def _payload(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):  # 코드펜스는 금지지만 오면 벗긴다
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
    try:
        obj = json.loads(raw)
    except ValueError as e:
        raise ExtractionSchemaError(f"JSON 파싱 실패: {e}") from e
    if not isinstance(obj, dict):
        raise ExtractionSchemaError("최상위가 객체가 아니다")
    return obj


def parse(
    text: str, *, messages: list[SourceMessage]
) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """모델 출력 → (검증된 후보, [(본문, 폐기사유)]).

    스키마 자체가 깨지면 `ExtractionSchemaError`(전량 폐기). 개별 후보의 근거 문제는 그 후보만 버린다.
    """
    obj = _payload(text)
    items = obj.get("candidates")
    if not isinstance(items, list):
        raise ExtractionSchemaError("candidates가 배열이 아니다")

    by_id = {m.id: m for m in messages}
    out: list[Candidate] = []
    dropped: list[tuple[str, str]] = []

    for item in items:
        if not isinstance(item, dict):
            raise ExtractionSchemaError(f"후보가 객체가 아니다: {item!r}")
        body = item.get("text")
        category = item.get("category")
        spans = item.get("evidence")
        if not isinstance(body, str) or not body.strip():
            raise ExtractionSchemaError("text가 비어 있다")
        if category not in CATEGORIES:
            raise ExtractionSchemaError(f"허용 밖 category: {category!r}")
        if not isinstance(spans, list) or not spans:
            dropped.append((body, "no_evidence"))
            continue

        verified: list[EvidenceSpan] = []
        problem: str | None = None
        for sp in spans:
            if not isinstance(sp, dict):
                raise ExtractionSchemaError(f"evidence 항목이 객체가 아니다: {sp!r}")
            mid, start, end = sp.get("message_id"), sp.get("start"), sp.get("end")
            if not all(isinstance(v, int) for v in (mid, start, end)):
                raise ExtractionSchemaError("evidence 좌표가 정수가 아니다")
            src = by_id.get(mid)
            if src is None:
                # 없는 메시지를 참조 — 모델이 지어냈다. 계약 위반이라 전량 폐기.
                raise ExtractionSchemaError(f"존재하지 않는 message_id: {mid}")
            if start < 0 or start >= end or end > len(src.utf8):
                problem = "span_out_of_range"
                break
            if src.sender != "user":
                problem = "assistant_evidence"
                break
            verified.append(
                EvidenceSpan(
                    message_id=mid, sender=src.sender, start_utf8=start, end_utf8=end,
                    content_hash=src.hash_of(start, end),
                )
            )
        if problem:
            dropped.append((body, problem))
            continue
        out.append(Candidate(text=body, evidence=tuple(verified), category=category))

    return out, dropped
