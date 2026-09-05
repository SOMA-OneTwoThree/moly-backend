"""대화 요약 checkpoint(W11) — **순수 로직과 LLM 호출**. SQL·트랜잭션은 `checkpoint_repo`.

지금은 앵커가 리셋될 때 그 앞 구간을 통째로 버린다 → 캐피가 앞 얘기를 잊는다. W11은 버리기 전에
그 구간을 요약해 checkpoint로 남기고, 다음 턴은 **최신 checkpoint 하나 + 그 이후 메시지**만 쓴다.

여기서 지키는 것(어기면 조용히 깨진다):

1. **`source_hash`는 결정적**이다. 이전 checkpoint의 `(id, source_hash)`와 이번 원본 메시지의
   정렬된 `(id, sender, kind, content)`를 **길이-prefix**로 직렬화해 SHA-256한다. 길이 prefix가
   없으면 필드 경계가 모호해져 서로 다른 입력이 같은 해시를 낼 수 있다(`a|bc` vs `ab|c`).
2. **요약 입력은 저장 표면(placeholder) 그대로**다. `naming.render`로 실명을 되살려 넣으면 그 이름이
   요약 문자열로 돌아와 저장 표면에 남는다(§0.2 실명 저장 금지). 저장 직전 `to_placeholder`를 한 번
   더 태우고 **실명 stem 회귀 검사**까지 통과해야 저장한다.
3. **Summary는 Fact가 아니다.** checkpoint나 요약 결과에서 장기 사실 extraction 잡을 만들지 않는다
   (§W11-7). 추출은 원본 `conversation_turn` evidence에서만 한다 — 요약을 사실로 되먹이면 W8의
   evidence 계약이 요약으로 오염된다.
4. **트리거·보존 값은 현행 설정을 그대로 쓴다.** `context_reset_*`(트리거)와 `context_keep_*`(보존
   tail)은 앵커 리셋이 이미 쓰는 값이고, 여기서 새 값을 만들면 "요약이 덮는 구간"과 "프롬프트에
   남는 구간"이 어긋나 겹치거나 구멍이 생긴다.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.config import settings
from app.services import i18n, llm, memory, naming, usage_ledger

_log = logging.getLogger("moly-backend")

# 잡·payload·요약기 계약 버전. payload 구조가 바뀌면 SCHEMA를, 프롬프트/출력 규약이 바뀌면
# SUMMARIZER를 올린다(SUMMARIZER는 잡 dedup key의 일부라 올리는 순간 같은 구간이 다시 요약된다).
JOB_CONVERSATION_CHECKPOINT = "conversation_checkpoint"
# v2 = payload에 `memory_generation`이 들어간다(잊어줘 이후 늦게 도착한 잡을 stale로 끊는 세대 검사).
SCHEMA_VERSION = "conversation-checkpoint-payload-v2"
SUMMARIZER_VERSION = "conversation-checkpoint-summary-v3"

# W1 회계의 호출 목적 라벨(LlmCall.purpose). 대화 턴 밖(워커)에서 도는 호출이라 chat 턴 사용량과
# 섞이지 않게 자체 라벨을 쓴다.
PURPOSE_CHECKPOINT = "checkpoint_summary"

# 요약 1회 **출력 예산**. 저장 길이 상한은 `SUMMARY_MAX_CHARS`가 따로 강제하므로, 이 값은
# 답변 길이가 아니라 "추론 + 답변"이 함께 들어갈 자리를 뜻한다.
#
# ⚠️ 400이면 **긴 구간에서 빈 요약이 나온다**(2026-08-05 dev 실측). 추론 모델이 예산 400을
#    전부 추론에 쓰고 답변 자리가 0이 되기 때문이다 — 오류가 아니라 빈 문자열로 돌아와서
#    `mask_summary`가 "요약이 비었다"로 거부하고, 재시도 3회가 전부 같은 이유로 실패한 뒤
#    잡이 dead가 된다. 즉 **대화가 길어진 사용자만 checkpoint가 영영 안 생긴다.**
#
#    실측(원문 74건·4,050자 입력, 같은 system 프롬프트):
#      max_tokens=400  → output 400, 텍스트 0자   ← 예산을 추론이 다 씀
#      max_tokens=1000 → output 394, 텍스트 540자
#      max_tokens=2000 → output 461, 텍스트 491자
#    실제 사용량은 ~460이라 2000으로 올려도 청구는 거의 그대로다. 오히려 실패 재시도 3회가
#    사라져 줄어든다.
SUMMARY_MAX_OUTPUT_TOKENS = 2_000
SUMMARY_MAX_CHARS = 1_200


class CheckpointError(Exception):
    """checkpoint 계약 위반 — 저장하지 않는다."""


class NameLeakError(CheckpointError):
    """마스킹 후에도 실명 stem이 남았다. 저장 표면에 실명을 남기느니 요약을 버린다."""


@dataclass(frozen=True, slots=True)
class SourceMessage:
    """요약 입력 메시지 1건. `content`는 DB 저장 표면(placeholder) 그대로다."""

    id: int
    sender: str   # user | moly
    kind: str     # normal | greeting
    content: str


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """저장된 checkpoint 1건(= 요약 체인의 한 마디)."""

    id: uuid.UUID
    through_message_id: int
    summary: str
    version: str
    source_hash: str


@dataclass(frozen=True, slots=True)
class CheckpointPlan:
    """"지금 무엇을 요약할지"의 확정본. 잡 payload와 dedup key가 여기서 나온다."""

    through_message_id: int
    source_message_ids: tuple[int, ...]
    source_hash: str
    previous_id: uuid.UUID | None
    previous_through_message_id: int | None
    version: str = SUMMARIZER_VERSION
    # 잡을 만든 시점의 `chat_contexts.memory_generation`. forget이 이 값을 +1 하므로, 늦게 실행된
    # 잡은 핸들러에서 세대 불일치로 끊긴다(잊어달라고 한 대화가 요약으로 되살아나지 못한다).
    memory_generation: int = 0

    def dedup_key(self, user_id: uuid.UUID | str) -> str:
        return dedup_key(
            user_id=user_id,
            through_message_id=self.through_message_id,
            source_hash=self.source_hash,
            version=self.version,
        )

    def payload(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "through_message_id": self.through_message_id,
            "source_message_ids": list(self.source_message_ids),
            "source_hash": self.source_hash,
            "summarizer_version": self.version,
            "previous_checkpoint_id": str(self.previous_id) if self.previous_id else None,
            "previous_through_message_id": self.previous_through_message_id,
            "memory_generation": self.memory_generation,
        }


# ─────────────────────────────────────────────────────────────
# 해시 — 결정적. 순서·경계가 다르면 반드시 다른 값이 나와야 한다.
# ─────────────────────────────────────────────────────────────
def _length_prefixed(*parts: str) -> bytes:
    """`<바이트길이>:<바이트>` 연결. 구분자를 쓰지 않아 본문에 구분자가 들어가도 경계가 흔들리지 않는다."""
    out = bytearray()
    for p in parts:
        b = p.encode("utf-8")
        out += str(len(b)).encode("ascii") + b":" + b
    return bytes(out)


def normalize_messages(messages: Sequence[SourceMessage]) -> tuple[SourceMessage, ...]:
    """id 오름차순 정렬 + 중복 id 거부. 정렬을 호출측에 맡기면 같은 구간이 다른 해시를 낸다."""
    ordered = sorted(messages, key=lambda m: m.id)
    ids = [m.id for m in ordered]
    if len(set(ids)) != len(ids):
        raise CheckpointError(f"요약 입력에 중복 message id가 있다: {ids}")
    if any(i <= 0 for i in ids):
        raise CheckpointError(f"message id는 양수여야 한다: {ids}")
    return tuple(ordered)


def source_hash(
    *, previous: Checkpoint | None, messages: Sequence[SourceMessage]
) -> str:
    """`(이전 checkpoint id, 이전 source_hash)` + 이번 원본 메시지의 길이-prefix SHA-256 hex.

    이전 checkpoint를 물리므로 체인 전체가 지문에 들어간다 — 앞 마디가 달라지면 뒤 마디도 다른
    해시다. 메시지 본문은 **저장 표면(placeholder)**을 그대로 넣는다(실명 렌더분 금지).
    """
    ordered = normalize_messages(messages)
    h = hashlib.sha256()
    # 개수 성분은 넣지 않는다 — 길이-prefix 직렬화가 이미 파트별 자기서술적이라(각 필드가 자기
    # 길이를 들고 있고 메시지당 필드 수가 4로 고정) 바이트열에서 파트 분해가 유일하다. 개수를
    # 더해도 구분력이 늘지 않는다.
    h.update(
        _length_prefixed(
            str(previous.id) if previous is not None else "",
            previous.source_hash if previous is not None else "",
        )
    )
    for m in ordered:
        h.update(_length_prefixed(str(m.id), m.sender, m.kind, m.content or ""))
    return h.hexdigest()


def dedup_key(
    *, user_id: uuid.UUID | str, through_message_id: int, source_hash: str, version: str
) -> str:
    """W7 `UNIQUE(job_type, dedup_key)`와 맞물려 같은 입력이 두 번 요약되지 않게 한다(§W11-3)."""
    return (
        f"user:{user_id}:through:{through_message_id}"
        f":source:{source_hash}:summarizer:{version}"
    )


# ─────────────────────────────────────────────────────────────
# 트리거·범위 — 현행 앵커 리셋과 **같은 설정값**을 쓴다(모듈 docstring 4).
# ─────────────────────────────────────────────────────────────
def should_checkpoint(messages: Sequence[SourceMessage]) -> bool:
    """세그먼트가 리셋 트리거(`context_reset_messages` 또는 `context_reset_chars`)에 닿았나."""
    if len(messages) >= settings.context_reset_messages:
        return True
    return sum(len(m.content or "") for m in messages) >= settings.context_reset_chars


def keep_tail(messages: Sequence[SourceMessage]) -> tuple[SourceMessage, ...]:
    """뒤에서부터 `context_keep_messages`·`context_keep_chars`를 **둘 다** 만족하는 최신 tail.

    `chat._keep_window`와 같은 규칙이다(상한에 닿으면 더 담지 않는다). 그 함수를 import하지 않는 건
    순환 import를 만들지 않기 위해서다 — 값은 같은 설정에서 읽으므로 갈라지지 않는다.
    """
    kept: list[SourceMessage] = []
    chars = 0
    for m in reversed(normalize_messages(messages)):
        if len(kept) >= settings.context_keep_messages or chars >= settings.context_keep_chars:
            break
        kept.append(m)
        chars += len(m.content or "")
    kept.reverse()
    return tuple(kept)


def plan(
    messages: Sequence[SourceMessage],
    *,
    previous: Checkpoint | None,
    keep_from_message_id: int | None = None,
    memory_generation: int = 0,
) -> CheckpointPlan | None:
    """이번 세그먼트로 만들 checkpoint 계획. 만들 게 없으면 None.

    - 트리거에 닿지 않았으면 None(§W11-1).
    - 보존 tail 직전 마지막 메시지가 `through_message_id`다(§W11-2).
    - 이전 checkpoint보다 앞으로 나갈 메시지가 없으면 만들지 않는다(§W11-2).

    `keep_from_message_id`가 주어지면 그 id부터를 보존 tail로 본다. **챗 배선은 이 값을 반드시
    넘긴다** — 앵커 리셋이 정한 새 앵커를 그대로 주면 요약 경계(`through = 앵커 - 1`)와 프롬프트에
    남는 구간(앵커 이후)이 정확히 맞물려 겹침도 빈틈도 없다. 특히 `chat._keep_window`는 Anthropic
    첫-메시지-user 계약 때문에 tail 앞머리의 캐피 메시지를 pop하는데, 그 결과가 곧 앵커라서 여기에
    그 규칙을 복제하지 않고도 두 경계가 자동으로 일치한다.

    값이 없으면(워커·단위 테스트) `keep_tail`로 자체 계산한다 — 그쪽엔 pop 규칙이 없으므로 배선
    경로와 경계가 한 칸 다를 수 있고, 그래서 챗은 항상 앵커를 넘긴다.

    `memory_generation`은 잡을 만드는 시점의 `chat_contexts.memory_generation`이다(§forget 계약).
    **producer(`checkpoint_repo.maybe_enqueue`)가 반드시 실제 값을 넘긴다** — 기본값 0은 순수 계획
    로직만 보는 호출(단위 테스트)용이다.
    """
    ordered = normalize_messages(messages)
    if not should_checkpoint(ordered):
        return None
    if keep_from_message_id is not None:
        head = tuple(m for m in ordered if m.id < keep_from_message_id)
    else:
        tail = keep_tail(ordered)
        head = ordered[: len(ordered) - len(tail)]
    if previous is not None:  # 이전 checkpoint가 이미 덮은 구간은 다시 요약하지 않는다
        head = tuple(m for m in head if m.id > previous.through_message_id)
    if not head:
        # tail 하나가 세그먼트 전체이거나(초장문 1건) 이전 checkpoint가 이미 여기까지 덮었다.
        return None
    return CheckpointPlan(
        through_message_id=head[-1].id,
        source_message_ids=tuple(m.id for m in head),
        source_hash=source_hash(previous=previous, messages=head),
        previous_id=previous.id if previous is not None else None,
        previous_through_message_id=(
            previous.through_message_id if previous is not None else None
        ),
        memory_generation=memory_generation,
    )


# ─────────────────────────────────────────────────────────────
# 요약 생성 — LLM 1회. 판정도 저장도 하지 않는다.
# ─────────────────────────────────────────────────────────────
_LANG_RULE = {
    "ko": "- 요약은 한국어로 쓴다.",
    "en": "- Write the summary in English.",
    "ja": "- 要約は日本語で書く。",
}


def build_system(language: str | None) -> str:
    """요약기 system 프롬프트. 페르소나(캐피)와 무관한 유틸리티 프롬프트다."""
    return (
        "너는 두 사람의 대화 기록을 뒤에 이어질 대화가 참고할 수 있게 압축하는 요약기다. "
        "대화에 답하지 말고 요약문만 출력한다.\n\n"
        "[규칙]\n"
        "- 사실·약속·감정 변화·이어질 이야깃거리를 남기고, 인사말과 잡담 반복은 버린다.\n"
        "- 대화에 나온 내용만 쓴다. 추측하거나 없는 사실을 만들지 않는다.\n"
        "- 사람 이름·호칭은 쓰지 않는다. {유저이름} 같은 토큰이 보이면 그대로 둔다.\n"
        "- 이전 요약이 함께 주어지면 그 내용을 이어받아 하나의 요약으로 합친다.\n"
        "- 자살·자해처럼 안전 확인이 필요했던 대화가 나온 뒤 다른 화제로 명확히 넘어갔다면, "
        "해당 위기 구간과 반복된 안전 질문을 요약에 남기지 않는다. 그 일이 있었다는 대체 문장도 "
        "만들지 않는다. 구간 끝까지 위기가 이어지면 최신 위기 발화와 안전 대응에 필요한 맥락을 "
        "생략하지 않는다.\n"
        f"{_LANG_RULE[i18n.resolve(language)]}\n"
        f"- 전체 {SUMMARY_MAX_CHARS}자 이내의 줄글로 쓰고, 제목·머리말·코드펜스는 붙이지 않는다."
    )


def render_conversation(messages: Sequence[SourceMessage]) -> str:
    """`#id [역할] 본문` 줄로 렌더. 본문은 살균해서 넣는다 — 유저가 대화 안에서 이 프레이밍을
    흉내내 지시를 위조하지 못하게(기억 추출과 같은 방어선)."""
    lines = []
    for m in normalize_messages(messages):
        role = "상대" if m.sender == "moly" else "유저"
        lines.append(f"#{m.id} [{role}] {memory.sanitize_text(m.content or '')}")
    return "\n".join(lines)


def build_user_prompt(
    messages: Sequence[SourceMessage], *, previous_summary: str | None
) -> str:
    """이전 요약(있으면) + 이번 원본 구간. 이전 요약도 살균해서 넣는다(저장분이라 이미 마스킹됨)."""
    convo = render_conversation(messages)
    if not previous_summary:
        return f"[대화]\n{convo}"
    return f"[이전 요약]\n{memory.sanitize_text(previous_summary)}\n\n[대화]\n{convo}"


def mask_summary(text: str, nickname: str | None) -> str:
    """저장 직전 강제 — 마스킹 → 살균 → 길이 클램프, 그리고 **실명 stem 회귀 검사**.

    검사는 마스킹의 멱등성으로 한다: 이미 마스킹된 문자열에 `to_placeholder`를 다시 걸어 값이
    바뀌면 첫 통과가 놓친 실명이 남아 있다는 뜻이므로 저장하지 않는다(naming의 경계 규칙과 같은
    방어선을 쓴다 — 여기에 자체 이름 탐지기를 새로 만들면 규칙이 두 벌이 된다).
    """
    if not (text or "").strip():
        # provider가 빈 문자열을 준 경우다. 마스킹이 지운 것과 원인이 전혀 다르므로 구분한다 —
        # 섞어 놓으면 출력 예산 부족(추론이 예산을 다 씀)을 마스킹 문제로 오진하게 된다.
        raise CheckpointError("provider가 빈 응답을 줬다(출력 예산 부족 의심)")
    masked = memory.sanitize_text(naming.to_placeholder(text, nickname) or "")
    if masked and naming.to_placeholder(masked, nickname) != masked:
        raise NameLeakError("요약에 마스킹되지 않은 실명이 남았다")
    if not masked:
        raise CheckpointError("마스킹·살균 후 요약이 비었다")
    return masked[:SUMMARY_MAX_CHARS]


async def summarize(
    *,
    messages: Sequence[SourceMessage],
    previous_summary: str | None,
    language: str | None,
    nickname: str | None,
    model: str | None = None,
    timeout: float | None = None,
    ledger: usage_ledger.LedgerContext | None = None,
) -> tuple[str, llm.LlmCall]:
    """대화 구간 → 저장 가능한 요약 + 그 호출의 회계 단위.

    LLM 실패는 그대로 올린다(호출측이 재시도 분류를 정한다). 실명이 남으면 `NameLeakError`.
    """
    ordered = normalize_messages(messages)
    if not ordered:
        raise CheckpointError("요약할 메시지가 없다")
    used_model = model or settings.model_utility
    result = await llm.generate(
        build_system(language),
        [{"role": "user", "content": build_user_prompt(ordered, previous_summary=previous_summary)}],
        model=used_model,
        max_tokens=SUMMARY_MAX_OUTPUT_TOKENS,
        timeout=timeout if timeout is not None else settings.llm_timeout_s,
        ledger=usage_ledger.with_purpose(ledger, "context_summary"),
    )
    call = llm.LlmCall(
        provider=llm.provider_for(result.model or used_model),
        model=result.model or used_model,
        purpose=PURPOSE_CHECKPOINT,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_write_tokens=result.cache_write_tokens,
        billable=llm.billable_tokens(result),
    )
    _log.info(  # PII 없음(건수·토큰만) — 요약 비용/길이 관측용
        "대화 요약 — messages=%d prev=%s in=%d out=%d billable=%d",
        len(ordered), bool(previous_summary), call.input_tokens, call.output_tokens, call.billable,
    )
    return mask_summary(result.text, nickname), call
