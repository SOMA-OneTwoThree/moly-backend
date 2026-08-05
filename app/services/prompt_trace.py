"""shadow trace — 새 assembler의 프롬프트를 **실제 응답에 쓰지 않고** 계측만 한다.

기억 재설계 9단계(docs/capi-memory-ARCHITECTURE.md 15장 9번).

측정하는 것:
 · 직렬화 byte / 추정 token
 · **캐시 가능 프리픽스 비율** — 이 설계의 비용 주장이 성립하는지 판정하는 값
 · 배치를 바꿨을 때(휘발값을 앞으로) 캐시가 얼마나 깨지는지

캐시 가능 프리픽스 = 매 턴 바뀌지 않는 앞부분이다. OpenAI 자동 프리픽스 캐시는 **첫 번째로
달라지는 지점 이후를 전부 미캐시**로 만들기 때문에, 휘발 segment가 앞에 오면 그 뒤 전부가
매 턴 새로 청구된다.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.prompt_assembly import (
    CacheClass,
    PromptSegment,
    to_openai_messages,
)

# 캐시 읽기 가중치(입력 대비). config의 bill_weight_cache_read_openai와 같은 값이다.
CACHE_READ_WEIGHT = 0.1


@dataclass(frozen=True, slots=True)
class PromptTrace:
    total_bytes: int
    cacheable_bytes: int
    volatile_bytes: int
    message_count: int

    @property
    def cacheable_ratio(self) -> float:
        return self.cacheable_bytes / self.total_bytes if self.total_bytes else 0.0

    def weighted_cost_units(self, *, cache_read_weight: float = CACHE_READ_WEIGHT) -> float:
        """상대 비용 단위. 캐시 적중분은 가중치만큼만 청구된다."""
        return self.cacheable_bytes * cache_read_weight + self.volatile_bytes


def trace(ordered: list[PromptSegment]) -> PromptTrace:
    """정렬된 segment 목록의 캐시 구조를 잰다.

    캐시 가능 = 첫 CURRENT 등장 **이전**까지. 그 뒤는 매 턴 바뀌므로 전부 휘발로 센다.
    """
    msgs = to_openai_messages(ordered)
    total = sum(len(m["content"].encode("utf-8")) for m in msgs)

    first_volatile = next(
        (i for i, s in enumerate(ordered) if s.cache_class >= CacheClass.CURRENT), len(ordered)
    )
    cacheable = sum(
        len(s.content.encode("utf-8")) for s in ordered[:first_volatile]
    )
    return PromptTrace(
        total_bytes=total,
        cacheable_bytes=cacheable,
        volatile_bytes=max(0, total - cacheable),
        message_count=len(msgs),
    )


def compare_orderings(
    ordered: list[PromptSegment], bad_ordered: list[PromptSegment]
) -> dict[str, float]:
    """올바른 배치 vs 잘못된 배치의 상대 비용.

    `bad_ordered`는 휘발값을 최근 원문 앞에 둔 배치다 — 설계가 금지한 순서.
    """
    good, bad = trace(ordered), trace(bad_ordered)
    good_cost = good.weighted_cost_units()
    bad_cost = bad.weighted_cost_units()
    return {
        "good_cacheable_ratio": round(good.cacheable_ratio, 3),
        "bad_cacheable_ratio": round(bad.cacheable_ratio, 3),
        "cost_multiplier": round(bad_cost / good_cost, 2) if good_cost else 0.0,
    }
