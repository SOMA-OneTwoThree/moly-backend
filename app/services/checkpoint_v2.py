"""checkpoint v2 — cumulative window와 daily digest.

기억 재설계 8단계(docs/ARCHITECTURE-capi.md 8.3절).

두 종류는 **같은 source 계약과 권위 규칙**을 쓰지만 역할이 다르다.

`window` — 현재 대화의 연속성. **누적 체인**이다. 새 segment 원문과 직전 published window 요약을
  입력으로 새 한 건을 만들고 `previous_checkpoint_id`로 잇는다. 프롬프트에는 **최신 한 건만** 넣는다.
`daily_digest` — activity date 하나의 독립 요약. 시간 회상용이며 window 체인에 연결하지 않고
  다음 window의 사실 근거로도 쓰지 않는다. 날짜 PK로만 조회한다(별도 semantic index 없음).

이 구분이 없으면 최신 checkpoint 하나만 넣었을 때 오래된 줄거리가 사라지거나, daily 요약이
장기 사실로 되먹는다.

**두 범위를 구분한다.**
  `segment_*`  — 이번에 새로 요약한 원문 구간
  `coverage_*` — 체인 전체가 덮는 구간(window는 누적, digest는 segment와 같다)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

KIND_WINDOW = "window"
KIND_DAILY_DIGEST = "daily_digest"

# job이 먼저 끝나도 anchor를 즉시 줄이지 않는다 — hard threshold에서 CAS로 활성화한다(8.3절).
PUBLISH_READY = "ready"
PUBLISH_PUBLISHED = "published"
PUBLISH_SUPERSEDED = "superseded"


class CheckpointContractError(ValueError):
    """범위·체인 계약 위반 — 조용히 보정하지 않고 실패시킨다.

    보정하면 원문 anchor에 구멍이 생기거나 오래된 줄거리가 사라진다.
    """


@dataclass(frozen=True, slots=True)
class Ranges:
    """한 checkpoint가 덮는 두 범위."""

    segment_from: int
    segment_through: int
    coverage_from: int
    coverage_through: int


def window_ranges(
    *, segment_from: int, segment_through: int, previous: Ranges | None
) -> Ranges:
    """누적 window의 범위를 계산한다.

    coverage는 체인의 시작부터 이번 segment 끝까지다. 첫 window면 segment와 같다.
    직전 coverage와 이번 segment가 **연속이 아니면** 실패시킨다 — 사이 구간이 어디에도
    요약되지 않은 채 anchor만 전진하면 그 대화가 통째로 사라진다.
    """
    if segment_from > segment_through:
        raise CheckpointContractError(
            f"segment 범위가 뒤집혔다: {segment_from} > {segment_through}"
        )
    if previous is None:
        return Ranges(segment_from, segment_through, segment_from, segment_through)
    if segment_from <= previous.coverage_through:
        raise CheckpointContractError(
            f"segment가 직전 coverage와 겹친다: {segment_from} <= {previous.coverage_through}"
        )
    return Ranges(
        segment_from=segment_from,
        segment_through=segment_through,
        coverage_from=previous.coverage_from,
        coverage_through=segment_through,
    )


def digest_ranges(*, segment_from: int, segment_through: int) -> Ranges:
    """daily digest는 누적하지 않는다 — coverage == segment."""
    if segment_from > segment_through:
        raise CheckpointContractError(
            f"segment 범위가 뒤집혔다: {segment_from} > {segment_through}"
        )
    return Ranges(segment_from, segment_through, segment_from, segment_through)


@dataclass(frozen=True, slots=True)
class CheckpointRow:
    kind: str
    ranges: Ranges
    previous_checkpoint_id: str | None
    summary: str
    source_hash: str
    locale: str | None
    source_started_at: datetime | None
    source_ended_at: datetime | None
    activity_date_from: date | None
    activity_date_to: date | None
    publish_state: str = PUBLISH_PUBLISHED


def validate(row: CheckpointRow) -> None:
    """저장 전 계약 검사. 위반은 저장하지 않는다."""
    if row.kind not in (KIND_WINDOW, KIND_DAILY_DIGEST):
        raise CheckpointContractError(f"알 수 없는 kind: {row.kind!r}")
    if row.kind == KIND_DAILY_DIGEST:
        # digest가 체인에 붙으면 하루 요약이 장기 사실로 되먹는다.
        if row.previous_checkpoint_id is not None:
            raise CheckpointContractError("daily_digest는 window 체인에 연결할 수 없다")
        if row.ranges.coverage_from != row.ranges.segment_from:
            raise CheckpointContractError("daily_digest의 coverage는 segment와 같아야 한다")
        if row.activity_date_from is None or row.activity_date_from != row.activity_date_to:
            raise CheckpointContractError("daily_digest는 하루 범위여야 한다")
    else:
        if row.ranges.coverage_through != row.ranges.segment_through:
            raise CheckpointContractError("window coverage 끝은 segment 끝과 같아야 한다")
        if row.ranges.coverage_from > row.ranges.segment_from:
            raise CheckpointContractError("window coverage 시작이 segment보다 뒤일 수 없다")
    if not row.summary.strip():
        raise CheckpointContractError("빈 요약은 저장하지 않는다")


def anchor_after(row: CheckpointRow, *, next_retained_message_id: int) -> int:
    """이 checkpoint를 활성화했을 때의 새 anchor.

    coverage-through는 **마지막으로 요약·제외되는** 메시지이고, anchor는 그 뒤 **첫 남는**
    메시지다. 전역 id의 `+1`을 가정하지 않고 호출측이 실제 다음 메시지를 넘긴다(8.3절).
    """
    if next_retained_message_id <= row.ranges.coverage_through:
        raise CheckpointContractError(
            f"anchor({next_retained_message_id})가 coverage_through"
            f"({row.ranges.coverage_through}) 이하다 — 요약된 구간을 다시 붙잡는다"
        )
    return next_retained_message_id
