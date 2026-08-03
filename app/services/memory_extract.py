"""턴 대화 → 후보 추출(W8) — LLM 호출과 payload 왕복만. **판정도 저장도 하지 않는다.**

경계를 좁게 잡는 이유:

1. LLM은 **후보만** 낸다. `proposed_action`은 관측용이고 ADD/SUPERSEDE 판정은 `memory_reconcile`이
   코드로 정한다. 여기서 판정을 흉내내면 두 벌의 규칙이 생긴다.
2. 검증은 `memory_candidates.parse_candidates` 한 곳에서만 한다. 자유문 파서를 따로 두면
   스키마가 갈라져 "여기선 통과, 저기선 실패"가 된다 — 그래서 모델 출력은 JSON 객체 하나로 못 박고,
   그 dict를 그대로 파서에 넘긴다.
3. 대화 원문은 **저장 표면 그대로**(placeholder) 넘긴다. 실명을 렌더해 넣으면 그 이름이 후보
   문자열로 되돌아와 저장 표면에 남는다(§0.2 실명 저장 금지).
4. 잡 payload로 실어 보낼 때도 **파싱을 통과한(마스킹된) 후보**를 다시 직렬화한다. 모델 원문을
   그대로 실으면 마스킹 전 문자열이 `async_jobs.payload`에 영구히 남는다.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.services import i18n, llm, memory, memory_registry
from app.services.memory_candidates import (
    SCHEMA_VERSION,
    Candidate,
    CandidateSchemaError,
    parse_candidates,
)

_log = logging.getLogger("moly-worker")

# 추출 1회 출력 상한. 후보 수십 건짜리 폭주 응답을 자르는 안전장치이며(잘리면 JSON 파싱이
# 깨져 스키마 실패로 떨어진다) 품질 근거가 있는 값은 아니다 — **shadow 분포 보고 조정 필요**.
MAX_OUTPUT_TOKENS = 1500


@dataclass(frozen=True, slots=True)
class SourceMessage:
    """추출에 넣는 메시지 1건. `content`는 DB 저장 표면(placeholder) 그대로다."""

    id: int
    sender: str
    content: str


def _vocabulary() -> str:
    """kind·predicate 목록을 registry에서 그대로 만든다 — 프롬프트가 registry와 따로 늙지 않게."""
    single = sorted(p for p, c in memory_registry.PREDICATES.items() if c == memory_registry.SINGLE)
    multi = sorted(p for p, c in memory_registry.PREDICATES.items() if c == memory_registry.MULTI)
    return (
        f"- kind(필수): {'|'.join(sorted(memory_registry.KINDS))}\n"
        f"- predicate(선택, 값이 하나뿐인 것): {'|'.join(single)}\n"
        f"- predicate(선택, 여러 개 공존 가능): {'|'.join(multi)}"
    )


# canonical_text를 유저 언어로 쓰게 한다(SOMA-365와 같은 근거 — 번역 저장 방지).
_LANG_RULE = {
    "ko": "- canonical_text는 한국어로 짧게 쓴다.",
    "en": "- Write canonical_text in short English.",
    "ja": "- canonical_text は短い日本語で書く。",
}


def build_system(language: str | None) -> str:
    """추출기 system 프롬프트. 페르소나(캐피)와 무관한 별도 유틸리티 프롬프트다."""
    return (
        "너는 대화에서 오래 기억할 만한 사실 후보만 뽑아내는 추출기다. "
        "대화에 답하지 말고 JSON 객체 하나만 출력한다.\n\n"
        "[출력 형식]\n"
        f'{{"schema_version":"{SCHEMA_VERSION}","candidates":[후보...]}}\n'
        '후보 = {"kind":"...","canonical_text":"...","importance":0~1,"confidence":0~1,'
        '"evidence_message_ids":[정수],"subject":null,"predicate":null,"object_json":null,'
        '"event_time":null}\n\n'
        "[어휘]\n"
        f"{_vocabulary()}\n\n"
        "[규칙]\n"
        "- predicate와 object_json은 둘 다 쓰거나 둘 다 null로 둔다.\n"
        "- evidence_message_ids는 아래 대화에 있는 #번호만 쓰고, 비워 두지 않는다.\n"
        "- 이번 대화에서 새로 드러난 사실만 뽑는다. 추측·요약·상대(assistant)의 말버릇은 후보가 아니다.\n"
        "- 뽑을 사실이 없으면 candidates를 빈 배열로 둔다. 억지로 만들지 않는다.\n"
        "- 사람 이름·호칭은 쓰지 않는다. {유저이름} 같은 토큰이 보이면 그대로 둔다.\n"
        "- event_time은 offset이 있는 RFC 3339만 쓰고, 모르면 null로 둔다.\n"
        f"{_LANG_RULE[i18n.resolve(language)]}\n"
        "- 설명도 코드펜스도 붙이지 말고 JSON 객체만 출력한다."
    )


def render_conversation(messages: Sequence[SourceMessage]) -> str:
    """`#id [역할] 본문` 줄로 렌더. 본문은 살균해서 넣는다 — 유저가 대화 안에서 이 프레이밍을
    흉내내 evidence 번호나 지시를 위조하지 못하게(기억 문자열과 같은 방어선)."""
    lines = []
    for m in sorted(messages, key=lambda x: x.id):
        role = "상대" if m.sender == "moly" else "유저"
        lines.append(f"#{m.id} [{role}] {memory.sanitize_text(m.content or '')}")
    return "\n".join(lines)


def _payload_dict(text: str) -> dict:
    """모델 출력 → dict. JSON 객체 하나만 받는다(코드펜스만 관용).

    관용은 여기까지다. 문장 속에서 JSON을 긁어내는 자유문 파싱은 하지 않는다 — 그건 스키마를
    두 벌로 만드는 지름길이고, 형식을 못 지킨 출력은 후보 전량 폐기가 맞다.
    """
    body = text.strip()
    if body.startswith("```"):  # ```json ... ``` 펜스만 벗긴다
        body = body.split("\n", 1)[-1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(body)
    except ValueError as e:
        raise CandidateSchemaError(f"추출 출력이 JSON이 아니다: {e}") from e
    if not isinstance(parsed, dict):
        raise CandidateSchemaError(f"추출 출력이 JSON 객체가 아니다: {type(parsed).__name__}")
    return parsed


async def extract(
    *,
    messages: Sequence[SourceMessage],
    language: str | None,
    nickname: str | None,
    model: str | None = None,
    timeout: float | None = None,
) -> tuple[list[Candidate], llm.LLMResult]:
    """대화 턴 → 검증된 후보 목록 + 그 호출의 실측 usage.

    스키마 위반은 `CandidateSchemaError`로 올린다(후보 전량 폐기 — 부분 수용 금지).
    LLM 호출 실패는 그대로 올린다(호출측이 재시도 분류를 정한다).
    """
    if not messages:
        raise CandidateSchemaError("추출할 메시지가 없다")
    result = await llm.generate(
        build_system(language),
        [{"role": "user", "content": render_conversation(messages)}],
        model=model or settings.model_utility,
        max_tokens=MAX_OUTPUT_TOKENS,
        timeout=timeout if timeout is not None else settings.llm_timeout_s,
    )
    candidates = parse_candidates(
        _payload_dict(result.text),
        allowed_message_ids=[m.id for m in messages],
        nickname=nickname,
    )
    _log.info(  # PII 없음(건수·토큰만) — 추출 품질/비용 관측용
        "기억 추출 — messages=%d candidates=%d in=%d out=%d",
        len(messages), len(candidates), result.input_tokens, result.output_tokens,
    )
    return candidates, result


def _plain(value: Any) -> Any:
    """jsonb에 실을 수 있는 형태로. datetime·uuid만 문자열로 바꾸고 나머지는 그대로 둔다."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def to_payload(candidates: Sequence[Candidate]) -> dict:
    """검증·마스킹을 통과한 후보 → 잡 payload(`memory-candidate-v1`).

    reconcile 잡이 이 payload를 **같은 파서로 다시 검증**한다. 즉 저장되는 후보는 항상
    마스킹된 값이고, extract가 죽거나 재시도돼도 reconcile 쪽 계약은 변하지 않는다.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "candidates": [
            {
                "kind": c.kind,
                "canonical_text": c.canonical_text,
                "subject": c.subject,
                "predicate": c.predicate,
                "object_json": _plain(c.object_json),
                "event_time": c.event_time.isoformat() if c.event_time is not None else None,
                "importance": c.importance,
                "confidence": c.confidence,
                "evidence_message_ids": list(c.evidence_message_ids),
                "proposed_action": c.proposed_action,
                "proposed_target_fact_id": (
                    str(c.proposed_target_fact_id)
                    if c.proposed_target_fact_id is not None
                    else None
                ),
            }
            for c in candidates
        ],
    }
