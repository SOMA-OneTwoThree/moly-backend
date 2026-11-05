"""대화 요약 checkpoint(W11) — **순수 로직과 LLM 호출**. SQL·트랜잭션은 `checkpoint_repo`.

지금은 앵커가 리셋될 때 그 앞 구간을 통째로 버린다 → 캐피가 앞 얘기를 잊는다. W11은 버리기 전에
그 구간을 요약해 checkpoint로 남기고, 다음 턴은 **최신 checkpoint 하나 + 그 이후 메시지**만 쓴다.

여기서 지키는 것(어기면 조용히 깨진다):

1. **`source_hash`는 결정적**이다. 이전 checkpoint의 `(id, source_hash)`와 이번 원본 메시지의
   정렬된 `(id, sender, kind, content)`와 v4의 `created_at`을 **길이-prefix**로 직렬화해 SHA-256한다. 길이 prefix가
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
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from app.config import settings
from app.services import i18n, llm, memory, naming, usage_ledger

_log = logging.getLogger("moly-backend")

# 잡·payload·요약기 계약 버전. payload 구조가 바뀌면 SCHEMA를, 프롬프트/출력 규약이 바뀌면
# SUMMARIZER를 올린다(SUMMARIZER는 잡 dedup key의 일부라 올리는 순간 같은 구간이 다시 요약된다).
JOB_CONVERSATION_CHECKPOINT = "conversation_checkpoint"
# v2 = payload에 `memory_generation`이 들어간다(잊어줘 이후 늦게 도착한 잡을 stale로 끊는 세대 검사).
SCHEMA_VERSION = "conversation-checkpoint-payload-v2"
LEGACY_SUMMARIZER_VERSION = "conversation-checkpoint-summary-v3"
SUMMARIZER_VERSION = "conversation-checkpoint-summary-v4"
RETENTION_SUMMARIZER_VERSION = "conversation-checkpoint-summary-v5"
RETENTION_SOFT_SOURCE_BYTES = 4_096
# Enable only after every consumer understands v4; missing/invalid config stays on v3.
TEMPORAL_CONFIG_KEY = "context_checkpoint_temporal_enabled"

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


# 이 모듈의 한국어 고정 오류 문구 → 로그용 이유 코드. 앞부분만 비교한다(뒤에 id 목록이 붙는 문구가 있다).
_ERROR_REASON_PREFIXES = (
    ("provider가 빈 응답을 줬다", "empty_provider_output"),
    ("마스킹·살균 후 요약이 비었다", "empty_after_sanitize"),
    ("요약 길이 초과", "summary_too_long"),
    ("요약할 메시지가 없다", "no_messages"),
    ("요약할 허용 메시지가 없다", "no_allowed_messages"),
    ("요약 입력에 중복 message id가 있다", "duplicate_message_ids"),
    ("message id는 양수여야 한다", "nonpositive_message_ids"),
    ("구형 줄글을 구조화 상태의 원본으로 승격할 수 없다", "legacy_summary_not_promotable"),
    ("요약에 마스킹되지 않은 실명이 남았다", "name_leak"),
)
_ERROR_REASON_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def error_reason_code(exc: BaseException) -> str:
    """로그에 남겨도 되는 고정 이유 코드.

    영문 snake_case 코드는 그대로, 알려진 한국어 고정 문구는 대응 코드로, 나머지는 "other".
    오류 문구 원문은 내보내지 않는다(message id 목록 등이 섞일 수 있다).
    """
    message = exc.args[0] if exc.args and isinstance(exc.args[0], str) else ""
    if _ERROR_REASON_CODE.fullmatch(message):
        return message
    for prefix, code in _ERROR_REASON_PREFIXES:
        if message.startswith(prefix):
            return code
    return "other"


@dataclass(frozen=True, slots=True)
class SourceMessage:
    """요약 입력 메시지 1건. `content`는 DB 저장 표면(placeholder) 그대로다."""

    id: int
    sender: str   # user | moly
    kind: str     # normal | greeting
    content: str
    created_at: datetime | None = None
    start_utf8: int = 0
    end_utf8: int | None = None
    source_hash: str | None = None


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """저장된 checkpoint 1건(= 요약 체인의 한 마디)."""

    id: uuid.UUID
    through_message_id: int
    summary: str
    version: str
    source_hash: str
    state_json: dict | None = None
    validation_json: dict | None = None


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
    reason: str | None = None
    privacy_epoch: int | None = None
    baseline_binding: dict | None = None

    def dedup_key(self, user_id: uuid.UUID | str) -> str:
        return dedup_key(
            user_id=user_id,
            through_message_id=self.through_message_id,
            source_hash=self.source_hash,
            version=self.version,
        )

    def payload(self) -> dict:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "through_message_id": self.through_message_id,
            "source_message_ids": list(self.source_message_ids),
            "source_hash": self.source_hash,
            "summarizer_version": self.version,
            "previous_checkpoint_id": str(self.previous_id) if self.previous_id else None,
            "previous_through_message_id": self.previous_through_message_id,
            "memory_generation": self.memory_generation,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.privacy_epoch is not None:
            payload["privacy_epoch"] = self.privacy_epoch
        if self.baseline_binding is not None:
            payload["baseline_binding"] = json.loads(json.dumps(self.baseline_binding))
        return payload


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
    *, previous: Checkpoint | None, messages: Sequence[SourceMessage],
    version: str = SUMMARIZER_VERSION,
    preparation_id: uuid.UUID | None = None,
    baseline_binding: dict | None = None,
) -> str:
    """`(이전 checkpoint id, 이전 source_hash)` + 이번 원본 메시지의 길이-prefix SHA-256 hex.

    이전 checkpoint를 물리므로 체인 전체가 지문에 들어간다 — 앞 마디가 달라지면 뒤 마디도 다른
    해시다. 메시지 본문은 **저장 표면(placeholder)**을 그대로 넣는다(실명 렌더분 금지).
    """
    ordered = normalize_messages(messages)
    h = hashlib.sha256()
    if preparation_id is not None:
        h.update(_length_prefixed("retention-preparation-v1", str(uuid.UUID(str(preparation_id)))))
    if baseline_binding is not None:
        if version != RETENTION_SUMMARIZER_VERSION or preparation_id is not None:
            raise CheckpointError("compat_baseline_namespace")
        h.update(_length_prefixed("retention-compat-v1", json.dumps(
            baseline_binding, sort_keys=True, ensure_ascii=False, separators=(",", ":"))))
    # 개수 성분은 넣지 않는다 — 길이-prefix 직렬화가 이미 파트별 자기서술적이라(각 필드가 자기
    # 길이를 들고 있고 메시지당 필드 수가 버전별로 고정) 바이트열에서 파트 분해가 유일하다. 개수를
    # 더해도 구분력이 늘지 않는다.
    h.update(
        _length_prefixed(
            str(previous.id) if previous is not None else "",
            previous.source_hash if previous is not None else "",
        )
    )
    for m in ordered:
        h.update(_length_prefixed(str(m.id), m.sender, m.kind, m.content or ""))
        if version in {SUMMARIZER_VERSION, RETENTION_SUMMARIZER_VERSION}:
            h.update(_length_prefixed(_source_time(m)))
    if version == RETENTION_SUMMARIZER_VERSION and previous is not None:
        h.update(_length_prefixed(json.dumps(previous.state_json, sort_keys=True,
                                             ensure_ascii=False, separators=(",", ":"))))
    return h.hexdigest()


def _source_time(message: SourceMessage) -> str:
    if message.created_at is None:
        return "time unknown"
    at = message.created_at
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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
def should_checkpoint(
    messages: Sequence[SourceMessage], *, retention_enabled: bool | None = None,
) -> bool:
    """세그먼트가 리셋 트리거(`context_reset_messages` 또는 `context_reset_chars`)에 닿았나."""
    enabled = (settings.context_retention_v2_enabled if retention_enabled is None
               else retention_enabled)
    if enabled:
        from app.services.context_retention import source_tokens
        return source_tokens(messages) >= RETENTION_SOFT_SOURCE_BYTES
    if len(messages) >= settings.context_reset_messages:
        return True
    return sum(len(m.content or "") for m in messages) >= settings.context_reset_chars


def keep_tail(
    messages: Sequence[SourceMessage], *, retention_enabled: bool = False,
) -> tuple[SourceMessage, ...]:
    """뒤에서부터 `context_keep_messages`·`context_keep_chars`를 **둘 다** 만족하는 최신 tail.

    `chat._keep_window`와 같은 규칙이다(상한에 닿으면 더 담지 않는다). 그 함수를 import하지 않는 건
    순환 import를 만들지 않기 위해서다 — 값은 같은 설정에서 읽으므로 갈라지지 않는다.
    """
    kept: list[SourceMessage] = []
    if retention_enabled:
        from app.services.context_retention import source_tokens
        for message in reversed(normalize_messages(messages)):
            kept.append(message)
            if source_tokens(kept) >= 2_048 and message.sender == "user":
                break
        return tuple(reversed(kept))
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
    reset_triggered: bool = False,
    version: str = SUMMARIZER_VERSION,
    through_message_id: int | None = None,
    reason: str | None = None,
    preparation_id: uuid.UUID | None = None,
    baseline_binding: dict | None = None,
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

    `reset_triggered=True`는 호출측이 **필터 전 원본 세그먼트로 이미 리셋을 확정한 경우**다.
    장기 기억에서 제외할 행을 제거한 뒤 메시지 수·글자 수를 다시 세어 정상 head의 checkpoint를
    놓치지 않게 한다. 경계는 반드시 같은 리셋에서 나온 `keep_from_message_id`와 함께 넘긴다.

    `memory_generation`은 잡을 만드는 시점의 `chat_contexts.memory_generation`이다(§forget 계약).
    **producer(`checkpoint_repo.maybe_enqueue`)가 반드시 실제 값을 넘긴다** — 기본값 0은 순수 계획
    로직만 보는 호출(단위 테스트)용이다.
    """
    ordered = normalize_messages(messages)
    retention_enabled = version == RETENTION_SUMMARIZER_VERSION
    if baseline_binding is not None:
        after = baseline_binding.get("source_after") if isinstance(baseline_binding, dict) else None
        if (not retention_enabled or preparation_id is not None or type(after) is not int or after < 0
                or type(baseline_binding.get("generation")) is not int
                or baseline_binding.get("generation") != memory_generation):
            raise CheckpointError("compat_baseline_binding")
        if previous is not None and (previous.validation_json or {}).get("baseline_binding") != baseline_binding:
            raise CheckpointError("compat_baseline_parent")
        ordered = tuple(message for message in ordered if message.id > after)
    pressure = reason == "input_pressure"
    if reason not in {None, "input_pressure"} or (pressure and not retention_enabled):
        raise CheckpointError("checkpoint_reason")
    if pressure and (type(through_message_id) is not int or through_message_id <= 0
                     or through_message_id not in {m.id for m in ordered}):
        raise CheckpointError("pressure_source_boundary")
    if through_message_id is not None and not pressure:
        raise CheckpointError("unexpected_source_boundary")
    if not pressure and not reset_triggered and not should_checkpoint(ordered, retention_enabled=retention_enabled):
        return None
    if pressure:
        head = tuple(m for m in ordered if m.id <= through_message_id)
    elif keep_from_message_id is not None:
        head = tuple(m for m in ordered if m.id < keep_from_message_id)
    else:
        tail = keep_tail(ordered, retention_enabled=retention_enabled)
        head = ordered[: len(ordered) - len(tail)]
    if previous is not None:  # 이전 checkpoint가 이미 덮은 구간은 다시 요약하지 않는다
        head = tuple(m for m in head if m.id > previous.through_message_id)
    if not head:
        # tail 하나가 세그먼트 전체이거나(초장문 1건) 이전 checkpoint가 이미 여기까지 덮었다.
        return None
    if retention_enabled and not pressure:
        from app.services.context_retention import source_tokens
        if source_tokens(head) < 1_024:
            return None
    return CheckpointPlan(
        through_message_id=head[-1].id,
        source_message_ids=tuple(m.id for m in head),
        source_hash=source_hash(previous=previous, messages=head, version=version,
                                preparation_id=preparation_id, baseline_binding=baseline_binding),
        previous_id=previous.id if previous is not None else None,
        previous_through_message_id=(
            previous.through_message_id if previous is not None else None
        ),
        memory_generation=memory_generation,
        baseline_binding=baseline_binding,
        reason=reason,
        version=version,
    )


# ─────────────────────────────────────────────────────────────
# 요약 생성 — LLM 1회. 판정도 저장도 하지 않는다.
# ─────────────────────────────────────────────────────────────
_LANG_RULE = {
    "ko": "- 요약은 한국어로 쓴다.",
    "en": "- Write the summary in English.",
    "ja": "- 要約は日本語で書く。",
}


def build_system(language: str | None, *, version: str = SUMMARIZER_VERSION) -> str:
    """요약기 system 프롬프트. 페르소나(캐피)와 무관한 유틸리티 프롬프트다."""
    temporal_rule = (
        "- 시각은 발화 시각이며 사건 시각과 다를 수 있다. 일시 상태는 필요한 경우에만 "
        "시점을 붙여 과거형으로 남긴다. 시점 없는 이전 요약을 현재 상태로 바꾸지 않는다. "
        "이전 요약의 시점을 보존하고 상태의 지속·종료를 추측하지 않는다.\n"
        if version == SUMMARIZER_VERSION else ""
    )
    return (
        "너는 두 사람의 대화 기록을 뒤에 이어질 대화가 참고할 수 있게 압축하는 요약기다. "
        "대화에 답하지 말고 요약문만 출력한다.\n\n"
        "[규칙]\n"
        "- 사실·약속·감정 변화·이어질 이야깃거리를 남기고, 인사말과 잡담 반복은 버린다.\n"
        "- 대화에 나온 내용만 쓴다. 추측하거나 없는 사실을 만들지 않는다.\n"
        "- 사람 이름·호칭은 쓰지 않는다. {유저이름} 같은 토큰이 보이면 그대로 둔다.\n"
        "- 이전 요약이 함께 주어지면 그 내용을 이어받아 하나의 요약으로 합친다.\n"
        f"{temporal_rule}"
        "- 자살·자해처럼 안전 확인이 필요했던 대화가 나온 뒤 다른 화제로 명확히 넘어갔다면, "
        "해당 위기 구간과 반복된 안전 질문을 요약에 남기지 않는다. 그 일이 있었다는 대체 문장도 "
        "만들지 않는다. 구간 끝까지 위기가 이어지면 최신 위기 발화와 안전 대응에 필요한 맥락을 "
        "생략하지 않는다.\n"
        f"{_LANG_RULE[i18n.resolve(language)]}\n"
        f"- 전체 {SUMMARY_MAX_CHARS}자 이내의 줄글로 쓰고, 제목·머리말·코드펜스는 붙이지 않는다."
    )


def render_conversation(
    messages: Sequence[SourceMessage], *, version: str = SUMMARIZER_VERSION,
) -> str:
    """`#id [역할] 본문` 줄로 렌더. 본문은 살균해서 넣는다 — 유저가 대화 안에서 이 프레이밍을
    흉내내 지시를 위조하지 못하게(기억 추출과 같은 방어선)."""
    lines = []
    for m in normalize_messages(messages):
        role = "상대" if m.sender == "moly" else "유저"
        stamp = f" [{_source_time(m)}]" if version == SUMMARIZER_VERSION else ""
        lines.append(f"#{m.id}{stamp} [{role}] {memory.sanitize_text(m.content or '')}")
    return "\n".join(lines)


def build_user_prompt(
    messages: Sequence[SourceMessage], *, previous_summary: str | None,
    version: str = SUMMARIZER_VERSION,
) -> str:
    """이전 요약(있으면) + 이번 원본 구간. 이전 요약도 살균해서 넣는다(저장분이라 이미 마스킹됨)."""
    convo = render_conversation(messages, version=version)
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
    if len(masked) > SUMMARY_MAX_CHARS:
        raise CheckpointError("요약 길이 초과 — 원문을 절단해 저장하지 않는다")
    return masked


async def summarize(
    *,
    messages: Sequence[SourceMessage],
    previous_summary: str | None,
    language: str | None,
    nickname: str | None,
    model: str | None = None,
    timeout: float | None = None,
    ledger: usage_ledger.LedgerContext | None = None,
    version: str = SUMMARIZER_VERSION,
) -> tuple[str, llm.LlmCall]:
    """대화 구간 → 저장 가능한 요약 + 그 호출의 회계 단위.

    LLM 실패는 그대로 올린다(호출측이 재시도 분류를 정한다). 실명이 남으면 `NameLeakError`.
    """
    ordered = normalize_messages(messages)
    if not ordered:
        raise CheckpointError("요약할 메시지가 없다")
    used_model = model or settings.model_utility
    result = await llm.generate(
        build_system(language, version=version),
        [{"role": "user", "content": build_user_prompt(
            ordered, previous_summary=previous_summary, version=version,
        )}],
        model=used_model, reasoning_effort="none",
        max_tokens=SUMMARY_MAX_OUTPUT_TOKENS,
        timeout=timeout if timeout is not None else settings.llm_timeout_s,
        ledger=usage_ledger.with_purpose(ledger, "context_summary"),
    )
    llm.require_completed_text(result.text, result.finish_reason)
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


BACKGROUND_INPUT_LIMIT = 16_384
BACKGROUND_SEGMENT_BYTES = 2_048
PACKING_VERSION = "packed-v1"


class ProgressContinuation(CheckpointError):
    """중간 결과는 게시하지 않고 같은 원문·부모의 다음 작업으로 넘긴다."""

    def __init__(self, *, state: dict, next_chunk: int, manifest: str, billable: int,
                 page_proof: dict | None = None, before_state: dict | None = None,
                 page_start: int | None = None):
        super().__init__("progress_continuation")
        self.state, self.next_chunk, self.manifest, self.billable = state, next_chunk, manifest, billable
        self.page_proof, self.before_state, self.page_start = page_proof, before_state, page_start


class ExpandedProgressLength(CheckpointError):
    """No delta was applied; the fenced worker may persist a cap-one fallback."""
    def __init__(self, *, start_chunk, next_chunk, before_state, manifest, packing_chunk_cap, billable):
        super().__init__("packed_output_length")
        self.start_chunk, self.next_chunk = start_chunk, next_chunk
        self.consumed_atoms = next_chunk - start_chunk
        self.before_state, self.manifest = before_state, manifest
        self.packing_version, self.packing_chunk_cap = PACKING_VERSION, packing_chunk_cap
        self.billable = billable


@dataclass(frozen=True, slots=True)
class PackedProgressResult:
    summary: str
    state: dict
    call: llm.LlmCall
    page_proof: dict
    before_state: dict
    page_start: int

    def __iter__(self):
        return iter((self.summary, self.state, self.call))


@dataclass(frozen=True, slots=True)
class PackedBatch:
    messages: tuple[SourceMessage, ...]
    start_chunk: int
    next_chunk: int


def source_segments(message: SourceMessage, *, max_bytes: int = BACKGROUND_SEGMENT_BYTES) -> tuple[SourceMessage, ...]:
    """본문을 UTF-8 경계에서 연속 분할하고 전체 원문 지문·원본 오프셋을 보존한다."""
    if type(max_bytes) is not int or max_bytes < 4:
        raise CheckpointError("source_segment_budget")
    body = (message.content or "").encode("utf-8")
    origin = message.start_utf8
    if type(origin) is not int or origin < 0 or (origin and message.source_hash is None):
        raise CheckpointError("source_segment_origin")
    if message.end_utf8 is not None and message.end_utf8 != origin + len(body):
        raise CheckpointError("source_segment_extent")
    digest = message.source_hash or hashlib.sha256(body).hexdigest()
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise CheckpointError("source_segment_hash")
    if not body:
        return (replace(message, start_utf8=origin, end_utf8=origin, source_hash=digest),)
    result = []
    cursor = 0
    while cursor < len(body):
        end = min(cursor + max_bytes, len(body))
        try:
            piece = body[cursor:end].decode("utf-8")
        except UnicodeDecodeError as exc:
            end = cursor + exc.start
            piece = body[cursor:end].decode("utf-8")
        if end <= cursor:
            raise CheckpointError("source_segment_utf8")
        result.append(replace(message, content=piece, start_utf8=origin + cursor,
                              end_utf8=origin + end, source_hash=digest))
        cursor = end
    return tuple(result)


def progress_batches(
    messages: Sequence[SourceMessage], *, previous: Checkpoint | None,
    language: str | None, input_limit: int = BACKGROUND_INPUT_LIMIT,
) -> tuple[tuple[SourceMessage, ...], ...]:
    """원문을 자른 뒤 성공으로 간주하지 않는다. 모든 구간을 고정된 호출 계획에 포함한다."""
    from app.services import context_retention as retention

    system = retention.build_delta_system(language)
    state = previous.state_json if previous else None
    parent = previous.id if previous else None
    batches: list[tuple[SourceMessage, ...]] = []
    pending: list[SourceMessage] = []
    for message in normalize_messages(messages):
        for segment in source_segments(message):
            candidate = [*pending, segment]
            body = retention.build_delta_input(previous_state=state, previous_id=parent, messages=candidate)
            try:
                retention.assert_input_budget(system, [{"role": "user", "content": body}],
                    input_limit=input_limit, output_reserve=SUMMARY_MAX_OUTPUT_TOKENS)
                fits = True
            except retention.RetentionError:
                fits = False
            if pending and (not fits or any(m.id == segment.id for m in pending)
                            or sum(len(m.content.encode()) for m in candidate)>BACKGROUND_SEGMENT_BYTES):
                batches.append(tuple(pending))
                pending = []
                body = retention.build_delta_input(previous_state=state, previous_id=parent, messages=[segment])
                try:
                    retention.assert_input_budget(system, [{"role": "user", "content": body}],
                        input_limit=input_limit, output_reserve=SUMMARY_MAX_OUTPUT_TOKENS)
                    fits = True
                except retention.RetentionError:
                    fits = False
            if not fits:
                raise CheckpointError("progress_background_budget")
            pending.append(segment)
    if pending:
        batches.append(tuple(pending))
    return tuple(batches)


def _progress_manifest(batches, *, previous: Checkpoint | None, language: str | None) -> str:
    state_hash = hashlib.sha256(json.dumps(previous.state_json if previous else None,
        sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    plan = {"parent": str(previous.id) if previous else None, "state_hash": state_hash,
            "language": language, "model": settings.model_utility,
            "batches": [[{"id": m.id, "sender": m.sender, "kind": m.kind,
                "start_utf8": m.start_utf8, "end_utf8": m.end_utf8,
                "source_hash": m.source_hash, "chunk_hash": hashlib.sha256(m.content.encode()).hexdigest(),
                "created_at": _source_time(m)} for m in batch] for batch in batches]}
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _packing_options(version, cap) -> None:
    if version not in (None, PACKING_VERSION):
        raise CheckpointError("progress_packing_version")
    if cap is not None and (version is None or type(cap) is not int or cap < 1):
        raise CheckpointError("progress_packing_cap")


def _packed_delta_system(language) -> str:
    from app.services import context_retention as retention
    return retention.build_delta_system(language) + (
        " 현재 대화에서 진행 중인 지칭 대상의 이름이나 별칭이 명시적으로 바뀌면 reference 변경으로 처리한다. "
        "같은 대상을 새 이름으로 지칭하는 연결을 유지한다. 비슷한 이름이라는 이유만으로 같은 대상으로 추정하지 않는다. "
        "이전 이름은 과거 이력이나 근거로 남길 수 있지만 현재 이름으로 유지하지 않는다. "
        "영구 사용자 프로필이나 응답 선호를 대신 기록하지 않는다."
    )


def _packed_manifest(atoms, *, previous, language) -> str:
    return _digest({"packing_version": PACKING_VERSION,
        "atomic_manifest": _progress_manifest(atoms, previous=previous, language=language),
        "system_hash": hashlib.sha256(_packed_delta_system(language).encode()).hexdigest(),
        "input_limit": BACKGROUND_INPUT_LIMIT, "output_reserve": SUMMARY_MAX_OUTPUT_TOKENS,
        "atomic_bytes": BACKGROUND_SEGMENT_BYTES})


def _coalesce_atoms(atoms) -> tuple[SourceMessage, ...]:
    """Merge only adjacent original spans with identical source metadata."""
    merged = []
    for atom in atoms:
        if (not atom or len({m.id for m in atom}) != len(atom)
                or sum(len(m.content.encode("utf-8")) for m in atom) > BACKGROUND_SEGMENT_BYTES):
            raise CheckpointError("packed_atomic_plan")
        for message in atom:
            if type(message.id) is not int or message.id <= 0 or message.source_hash is None or message.end_utf8 is None:
                raise CheckpointError("packed_source_extent")
            # source_segments checks the full hash, absolute extent and UTF-8.
            checked = source_segments(message)
            if not checked:
                raise CheckpointError("packed_source_extent")
            if merged and message.id < merged[-1].id:
                raise CheckpointError("packed_source_order")
            if merged and message.id == merged[-1].id:
                last = merged[-1]
                if ((last.sender, last.kind, str(last.created_at), last.source_hash)
                        != (message.sender, message.kind, str(message.created_at), message.source_hash)
                        or last.end_utf8 != message.start_utf8):
                    raise CheckpointError("packed_source_contiguity")
                merged[-1] = replace(last, content=last.content + message.content,
                                     end_utf8=message.end_utf8)
            else:
                merged.append(message)
    return tuple(merged)


def packed_progress_batch(atoms, *, previous: Checkpoint | None, language: str | None,
                          state: dict, start_chunk: int, packing_chunk_cap: int | None = None) -> PackedBatch:
    """Largest consecutive atomic range fitting the actual working-state wire."""
    from app.services import context_retention as retention
    _packing_options(PACKING_VERSION, packing_chunk_cap)
    if type(start_chunk) is not int or not 0 <= start_chunk < len(atoms):
        raise CheckpointError("progress_packing_bounds")
    valid = retention.validate_state(state)
    stop = min(len(atoms), start_chunk + packing_chunk_cap) if packing_chunk_cap is not None else len(atoms)
    chosen = None
    for end in range(start_chunk + 1, stop + 1):
        messages = _coalesce_atoms(atoms[start_chunk:end])
        system = _packed_delta_system(language)
        convo = [{"role": "user", "content": retention.build_delta_input(
            previous_state=valid, previous_id=previous.id if previous else None,
            messages=messages, compact_previous=start_chunk == 0)}]
        try:
            retention.assert_input_budget(system, convo, input_limit=BACKGROUND_INPUT_LIMIT,
                                          output_reserve=SUMMARY_MAX_OUTPUT_TOKENS)
        except retention.RetentionError as exc:
            if chosen is None:
                raise CheckpointError("progress_background_budget") from exc
            break
        chosen = PackedBatch(messages, start_chunk, end)
    return chosen


def _packed_sources_hash(messages) -> str:
    return _digest([{"id": m.id, "sender": m.sender, "kind": m.kind,
        "created_at": str(m.created_at), "start_utf8": m.start_utf8, "end_utf8": m.end_utf8,
        "source_hash": m.source_hash, "content_hash": hashlib.sha256(m.content.encode()).hexdigest()}
        for m in messages])


def _packed_page_proof(*, batch, before, after, manifest, total, cap):
    proof = {"packing_version": PACKING_VERSION, "manifest": manifest,
        "start_chunk": batch.start_chunk, "next_chunk": batch.next_chunk, "total_chunks": total,
        "packing_chunk_cap": cap, "before_state": before, "before_state_hash": _digest(before),
        "after_state_hash": _digest(after), "source_span_hash": _packed_sources_hash(batch.messages)}
    return {**proof, "proof_hash": _digest(proof)}


_PACKED_PAGE_KEYS = frozenset(("packing_version", "manifest", "start_chunk", "next_chunk", "total_chunks",
    "packing_chunk_cap", "before_state", "before_state_hash", "after_state_hash", "source_span_hash", "proof_hash"))


def _check_packed_page_shape(proof, *, manifest, total):
    from app.services import context_retention as retention
    if (not isinstance(proof, dict) or set(proof) != _PACKED_PAGE_KEYS
            or proof.get("packing_version") != PACKING_VERSION or proof.get("manifest") != manifest
            or type(proof.get("total_chunks")) is not int or proof["total_chunks"] != total
            or any(type(proof.get(k)) is not int for k in ("start_chunk", "next_chunk"))
            or not 0 <= proof["start_chunk"] < proof["next_chunk"] <= total
            or proof.get("proof_hash") != _digest({k: v for k, v in proof.items() if k != "proof_hash"})):
        raise CheckpointError("progress_page_proof")
    _packing_options(PACKING_VERSION, proof.get("packing_chunk_cap"))
    try:
        before = retention.validate_state(proof["before_state"])
    except retention.RetentionError as exc:
        raise CheckpointError("progress_page_state") from exc
    if proof["before_state"] is None or _digest(before) != proof["before_state_hash"]:
        raise CheckpointError("progress_page_proof")


def _validate_packed_page(*, atoms, previous, language, state, previous_state,
                          start_chunk, next_chunk, manifest, page_proof, cap, completed=False):
    from app.services import context_retention as retention
    expected_manifest = _packed_manifest(atoms, previous=previous, language=language)
    _check_packed_page_shape(page_proof, manifest=expected_manifest, total=len(atoms))
    if (type(start_chunk) is not int or type(next_chunk) is not int
            or manifest != expected_manifest or page_proof["start_chunk"] != start_chunk
            or page_proof["next_chunk"] != next_chunk
            or (next_chunk == len(atoms)) != completed):
        raise CheckpointError("progress_page_proof")
    before = retention.validate_state(previous_state)
    after = retention.validate_state(state)
    parent_sequence = retention.validate_state(previous.state_json if previous else None)["sequence"]
    if before["sequence"] != parent_sequence or after["sequence"] != parent_sequence + completed:
        raise CheckpointError("progress_page_sequence")
    if start_chunk == 0 and before != retention.validate_state(previous.state_json if previous else None):
        raise CheckpointError("progress_page_origin")
    batch = packed_progress_batch(atoms, previous=previous, language=language, state=before,
                                  start_chunk=start_chunk, packing_chunk_cap=cap)
    expected = _packed_page_proof(batch=batch, before=before, after=after,
                                  manifest=expected_manifest, total=len(atoms), cap=cap)
    if batch.next_chunk != next_chunk or page_proof != expected:
        raise CheckpointError("progress_page_proof")


def completed_progress_proof(*, messages: Sequence[SourceMessage], previous: Checkpoint | None,
                             language: str | None, state: dict, packing_version: str | None = None,
                             packing_chunk_cap: int | None = None, page_proof: dict | None = None,
                             previous_state: dict | None = None, start_chunk: int = 0) -> dict:
    """Bind an unpublished, completed draft to its entire deterministic chunk plan."""
    from app.services import context_retention as retention

    _packing_options(packing_version, packing_chunk_cap)
    valid = retention.validate_state(state)
    batches = progress_batches(messages, previous=previous, language=language)
    if packing_version == PACKING_VERSION:
        manifest = _packed_manifest(batches, previous=previous, language=language)
        if batches:
            _validate_packed_page(atoms=batches, previous=previous, language=language, state=valid,
                previous_state=previous_state, start_chunk=start_chunk, next_chunk=len(batches),
                manifest=manifest, page_proof=page_proof, cap=packing_chunk_cap, completed=True)
        elif (page_proof is not None or type(start_chunk) is not int or start_chunk != 0
                or valid != retention.validate_state(previous.state_json if previous else None)
                or (previous_state is not None and retention.validate_state(previous_state) != valid)):
            raise CheckpointError("progress_completed_empty")
        retention.render_state(valid)
        return {"packing_version": PACKING_VERSION, "packing_chunk_cap": packing_chunk_cap,
                "manifest": manifest, "total_chunks": len(batches), "completed_chunks": len(batches),
                "state_hash": _digest(valid), "page_proof": page_proof}
    if page_proof is not None:
        raise CheckpointError("progress_packing_version")
    parent_sequence = retention.validate_state(previous.state_json if previous else None)["sequence"]
    if valid["sequence"] != parent_sequence + bool(batches):
        raise CheckpointError("progress_completed_sequence")
    # Render now as well as at publication: malformed/oversized drafts never become ready.
    retention.render_state(valid)
    return {"manifest": _progress_manifest(batches, previous=previous, language=language),
            "total_chunks": len(batches), "completed_chunks": len(batches),
            "state_hash": hashlib.sha256(json.dumps(valid, sort_keys=True, ensure_ascii=False,
                                                    separators=(",", ":")).encode()).hexdigest()}


def validate_progress_page(*, messages: Sequence[SourceMessage], previous: Checkpoint | None,
                           language: str | None, state: dict, next_chunk: int,
                           manifest: str, start_chunk: int, packing_version: str | None = None,
                           packing_chunk_cap: int | None = None, previous_state: dict | None = None,
                           page_proof: dict | None = None) -> None:
    """A durable intermediate page must match the frozen plan and exactly one completed call."""
    from app.services import context_retention as retention
    _packing_options(packing_version, packing_chunk_cap)
    batches = progress_batches(messages, previous=previous, language=language)
    if packing_version == PACKING_VERSION:
        _validate_packed_page(atoms=batches, previous=previous, language=language, state=state,
            previous_state=previous_state, start_chunk=start_chunk, next_chunk=next_chunk,
            manifest=manifest, page_proof=page_proof, cap=packing_chunk_cap)
        return
    if page_proof is not None:
        raise CheckpointError("progress_packing_version")
    parent_sequence = retention.validate_state(previous.state_json if previous else None)["sequence"]
    if (type(next_chunk) is not int or next_chunk != start_chunk + 1
            or not 0 < next_chunk < len(batches)
            or manifest != _progress_manifest(batches, previous=previous, language=language)
            or retention.validate_state(state)["sequence"] != parent_sequence):
        raise CheckpointError("progress_page_proof")


def validate_completed_progress(*, messages: Sequence[SourceMessage], previous: Checkpoint | None,
                                language: str | None, state: dict, proof: dict,
                                packing_version: str | None = None, packing_chunk_cap: int | None = None,
                                page_proof: dict | None = None, previous_state: dict | None = None,
                                start_chunk: int = 0) -> str:
    """Publication uses no model and accepts neither a partial draft nor a changed proof."""
    expected = completed_progress_proof(messages=messages, previous=previous, language=language,
        state=state, packing_version=packing_version, packing_chunk_cap=packing_chunk_cap,
        page_proof=page_proof, previous_state=previous_state, start_chunk=start_chunk)
    if (not isinstance(proof, dict) or set(proof) != set(expected)
            or type(proof.get("total_chunks")) is not int
            or type(proof.get("completed_chunks")) is not int or proof != expected):
        raise CheckpointError("progress_completed_proof")
    from app.services import context_retention as retention
    return retention.render_state(retention.validate_state(state))


async def summarize_progress(
    *, messages: Sequence[SourceMessage], previous: Checkpoint | None,
    language: str | None, nickname: str | None,
    ledger: usage_ledger.LedgerContext | None = None,
    timeout: float | None = None,
    continuation_state: dict | None = None,
    start_chunk: int = 0,
    continuation_manifest: str | None = None,
    max_batches: int = 1,
    packing_version: str | None = None,
    packing_chunk_cap: int | None = None,
    continuation_proof: dict | None = None,
    before_call: Callable[[], Awaitable[None]] | None = None,
) -> tuple[str, dict, llm.LlmCall] | PackedProgressResult:
    """v5: 이전 상태를 통째로 재작성하지 않고 검증된 변경만 합친다."""
    from app.services import context_retention as retention

    _packing_options(packing_version, packing_chunk_cap)
    if packing_version == PACKING_VERSION:
        return await _summarize_packed(messages=messages, previous=previous, language=language,
            nickname=nickname, ledger=ledger, timeout=timeout, continuation_state=continuation_state,
            start_chunk=start_chunk, continuation_manifest=continuation_manifest, max_batches=max_batches,
            packing_chunk_cap=packing_chunk_cap, continuation_proof=continuation_proof, before_call=before_call)
    if continuation_proof is not None:
        raise CheckpointError("progress_packing_version")
    ordered = normalize_messages(messages)
    if not ordered:
        raise CheckpointError("요약할 허용 메시지가 없다")
    if previous is not None and previous.version != RETENTION_SUMMARIZER_VERSION:
        raise CheckpointError("구형 줄글을 구조화 상태의 원본으로 승격할 수 없다")
    if type(start_chunk) is not int or start_chunk < 0 or type(max_batches) is not int or max_batches < 1:
        raise CheckpointError("progress_continuation_bounds")
    previous_state = previous.state_json if previous else None
    previous_id = previous.id if previous else None
    batches = progress_batches(ordered, previous=previous, language=language)
    manifest = _progress_manifest(batches, previous=previous, language=language)
    if start_chunk >= len(batches) or (start_chunk and (continuation_state is None or continuation_manifest != manifest)):
        raise CheckpointError("progress_continuation_manifest")
    if not start_chunk and (continuation_state is not None or continuation_manifest is not None):
        raise CheckpointError("progress_continuation_origin")
    state = retention.validate_state(continuation_state if start_chunk else previous_state)
    initial_sequence = retention.validate_state(previous_state)["sequence"]
    if state["sequence"] != initial_sequence:
        raise CheckpointError("progress_continuation_sequence")
    totals = dict(input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0, billable=0)
    model = settings.model_utility
    stop = min(len(batches), start_chunk + max_batches)
    for index in range(start_chunk, stop):
        batch = batches[index]
        system = retention.build_delta_system(language)
        convo = [{"role": "user", "content": retention.build_delta_input(
            previous_state=state, previous_id=previous_id, messages=batch,
            compact_previous=index == 0,
        )}]
        try:
            # 실제 draft 상태를 포함한 전체 입력을 매 호출 직전에 다시 검사한다.
            retention.assert_input_budget(system, convo, input_limit=BACKGROUND_INPUT_LIMIT,
                                          output_reserve=SUMMARY_MAX_OUTPUT_TOKENS)
        except retention.RetentionError as exc:
            raise CheckpointError("progress_background_budget") from exc
        if before_call is not None:
            await before_call()
        result = await llm.generate(system, convo, model=settings.model_utility,
            reasoning_effort="none", max_tokens=SUMMARY_MAX_OUTPUT_TOKENS,
            timeout=timeout if timeout is not None else settings.llm_timeout_s,
            ledger=usage_ledger.with_purpose(ledger, "context_summary"))
        llm.require_completed_text(result.text, result.finish_reason)
        try:
            state = retention.apply_delta(result.text, previous_state=state, previous_id=previous_id,
                messages=batch, nickname=nickname, compact_previous=index == 0,
                advance_checkpoint=index == len(batches)-1)
        except retention.RetentionError as exc:
            raise CheckpointError(str(exc)) from exc
        model = result.model or settings.model_utility
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            totals[key] += getattr(result, key)
        totals["billable"] += llm.billable_tokens(result)
    if stop < len(batches):
        raise ProgressContinuation(state=state, next_chunk=stop, manifest=manifest, billable=totals["billable"])
    call = llm.LlmCall(provider=llm.provider_for(model), model=model, purpose=PURPOSE_CHECKPOINT, **totals)
    return retention.render_state(state), state, call


async def _summarize_packed(*, messages, previous, language, nickname, ledger, timeout,
                            continuation_state, start_chunk, continuation_manifest, max_batches,
                            packing_chunk_cap, continuation_proof, before_call):
    from app.services import context_retention as retention

    ordered = normalize_messages(messages)
    if not ordered:
        raise CheckpointError("요약할 허용 메시지가 없다")
    if previous is not None and previous.version != RETENTION_SUMMARIZER_VERSION:
        raise CheckpointError("구형 줄글을 구조화 상태의 원본으로 승격할 수 없다")
    if type(start_chunk) is not int or start_chunk < 0 or type(max_batches) is not int or max_batches < 1:
        raise CheckpointError("progress_continuation_bounds")
    parent_state = previous.state_json if previous else None
    atoms = progress_batches(ordered, previous=previous, language=language)
    manifest = _packed_manifest(atoms, previous=previous, language=language)
    if start_chunk >= len(atoms):
        raise CheckpointError("progress_continuation_manifest")
    state = retention.validate_state(continuation_state if start_chunk else parent_state)
    if state["sequence"] != retention.validate_state(parent_state)["sequence"]:
        raise CheckpointError("progress_continuation_sequence")
    if start_chunk:
        if continuation_state is None or continuation_manifest != manifest:
            raise CheckpointError("progress_continuation_manifest")
        _check_packed_page_shape(continuation_proof, manifest=manifest, total=len(atoms))
        _validate_packed_page(atoms=atoms, previous=previous, language=language, state=state,
            previous_state=continuation_proof["before_state"], start_chunk=continuation_proof["start_chunk"],
            next_chunk=start_chunk, manifest=manifest, page_proof=continuation_proof,
            cap=continuation_proof["packing_chunk_cap"])
        if (continuation_proof["next_chunk"] != start_chunk
                or continuation_proof["after_state_hash"] != _digest(state)
                or continuation_proof["source_span_hash"] != _packed_sources_hash(_coalesce_atoms(
                    atoms[continuation_proof["start_chunk"]:start_chunk]))):
            raise CheckpointError("progress_continuation_proof")
        previous_cap = continuation_proof["packing_chunk_cap"]
        if previous_cap is not None and (packing_chunk_cap is None or packing_chunk_cap > previous_cap):
            raise CheckpointError("progress_packing_cap_increased")
    elif continuation_state is not None or continuation_manifest is not None or continuation_proof is not None:
        raise CheckpointError("progress_continuation_origin")

    totals = dict(input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0, billable=0)
    model, index, calls = settings.model_utility, start_chunk, 0
    while index < len(atoms) and calls < max_batches:
        before = retention.validate_state(state)
        batch = packed_progress_batch(atoms, previous=previous, language=language, state=before,
            start_chunk=index, packing_chunk_cap=packing_chunk_cap)
        system = _packed_delta_system(language)
        convo = [{"role": "user", "content": retention.build_delta_input(
            previous_state=before, previous_id=previous.id if previous else None,
            messages=batch.messages, compact_previous=index == 0)}]
        try:
            retention.assert_input_budget(system, convo, input_limit=BACKGROUND_INPUT_LIMIT,
                                          output_reserve=SUMMARY_MAX_OUTPUT_TOKENS)
        except retention.RetentionError as exc:
            raise CheckpointError("progress_background_budget") from exc
        if before_call is not None:
            await before_call()
        result = await llm.generate(system, convo, model=settings.model_utility,
            reasoning_effort="none", max_tokens=SUMMARY_MAX_OUTPUT_TOKENS,
            timeout=timeout if timeout is not None else settings.llm_timeout_s,
            ledger=usage_ledger.with_purpose(ledger, "context_summary"))
        if result.finish_reason == "length" and batch.next_chunk - index > 1:
            raise ExpandedProgressLength(start_chunk=index, next_chunk=batch.next_chunk,
                before_state=before, manifest=manifest, packing_chunk_cap=packing_chunk_cap,
                billable=totals["billable"] + llm.billable_tokens(result))
        llm.require_completed_text(result.text, result.finish_reason)
        try:
            state = retention.apply_delta(result.text, previous_state=before,
                previous_id=previous.id if previous else None, messages=batch.messages,
                nickname=nickname, compact_previous=index == 0,
                advance_checkpoint=batch.next_chunk == len(atoms))
        except retention.RetentionError as exc:
            raise CheckpointError(str(exc)) from exc
        page_proof = _packed_page_proof(batch=batch, before=before, after=state,
            manifest=manifest, total=len(atoms), cap=packing_chunk_cap)
        model = result.model or settings.model_utility
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            totals[key] += getattr(result, key)
        totals["billable"] += llm.billable_tokens(result)
        index, calls = batch.next_chunk, calls + 1
    if index < len(atoms):
        raise ProgressContinuation(state=state, next_chunk=index, manifest=manifest,
            billable=totals["billable"], page_proof=page_proof, before_state=before, page_start=batch.start_chunk)
    call = llm.LlmCall(provider=llm.provider_for(model), model=model, purpose=PURPOSE_CHECKPOINT, **totals)
    return PackedProgressResult(retention.render_state(state), state, call, page_proof, before, batch.start_chunk)
