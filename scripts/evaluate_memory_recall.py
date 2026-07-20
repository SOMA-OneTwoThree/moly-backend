"""고정 한국어 fixture로 semantic 기억 회상의 오프라인 임베딩 휴리스틱을 계산한다."""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from app.config import settings  # noqa: E402


DEFAULT_FIXTURE = REPOSITORY_ROOT / "evals/memory_recall_cases.json"
REQUIRED_CATEGORIES = {
    "relevant_fact",
    "older_than_20",
    "recent_irrelevant",
    "preference_update",
    "ambiguous_reference",
    "temporal",
    "multi_session",
    "no_match",
}
SEMANTIC_TOP_K = 4
LEGACY_LIMIT = 20
EMBEDDING_MODEL = "text-embedding-3-small"
MIN_RECALL = 0.90
MIN_IMPROVEMENT = 0.30
MAX_NO_MATCH_FALSE_SELECTION = 0.05
_HANGUL = re.compile(r"[가-힣]")


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    text: str
    created_at: str


@dataclass(frozen=True)
class RecallCase:
    id: str
    category: str
    query: str
    expected_memory_ids: tuple[str, ...]


@dataclass(frozen=True)
class Fixture:
    memories: tuple[MemoryRecord, ...]
    cases: tuple[RecallCase, ...]


@dataclass(frozen=True)
class Metrics:
    semantic_recall_at_4: float
    legacy_recall_at_20: float
    improvement: float
    no_match_false_selection_rate: float
    matched_cases: int
    no_match_cases: int


def load_fixture(path: Path = DEFAULT_FIXTURE) -> Fixture:
    raw = json.loads(path.read_text(encoding="utf-8"))
    memories = tuple(MemoryRecord(**item) for item in raw["memory_bank"])
    cases = tuple(
        RecallCase(
            id=item["id"],
            category=item["category"],
            query=item["query"],
            expected_memory_ids=tuple(item["expected_memory_ids"]),
        )
        for item in raw["cases"]
    )
    _validate_fixture(memories, cases)
    return Fixture(memories, cases)


def _validate_fixture(
    memories: tuple[MemoryRecord, ...], cases: tuple[RecallCase, ...]
) -> None:
    if len(cases) < 40:
        raise ValueError(f"memory recall fixture needs at least 40 cases, got {len(cases)}")
    memory_ids = [memory.id for memory in memories]
    case_ids = [case.id for case in cases]
    if len(memory_ids) != len(set(memory_ids)):
        raise ValueError("memory recall fixture contains duplicate memory IDs")
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("memory recall fixture contains duplicate case IDs")
    missing_categories = REQUIRED_CATEGORIES - {case.category for case in cases}
    if missing_categories:
        raise ValueError(f"memory recall fixture categories missing: {sorted(missing_categories)}")
    known_ids = set(memory_ids)
    for memory in memories:
        if not _HANGUL.search(memory.text):
            raise ValueError(f"memory {memory.id} is not Korean")
    for case in cases:
        if not _HANGUL.search(case.query):
            raise ValueError(f"case {case.id} query is not Korean")
        unknown = set(case.expected_memory_ids) - known_ids
        if unknown:
            raise ValueError(f"case {case.id} references unknown memories: {sorted(unknown)}")
        if case.category == "no_match" and case.expected_memory_ids:
            raise ValueError(f"no-match case {case.id} must not have expected memories")
        if case.category != "no_match" and not case.expected_memory_ids:
            raise ValueError(f"matched case {case.id} needs expected memories")


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding dimensions differ")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def embed_batch(client: Any, texts: list[str], model: str) -> dict[str, list[float]]:
    unique_texts = list(dict.fromkeys(texts))
    response = client.embeddings.create(model=model, input=unique_texts)
    rows = sorted(response.data, key=lambda row: row.index)
    if len(rows) != len(unique_texts):
        raise RuntimeError(
            f"embedding response count mismatch: expected {len(unique_texts)}, got {len(rows)}"
        )
    return {
        text: [float(value) for value in row.embedding]
        for text, row in zip(unique_texts, rows)
    }


def semantic_selection(
    case: RecallCase,
    memories: tuple[MemoryRecord, ...],
    embeddings: dict[str, list[float]],
    threshold: float,
    top_k: int = SEMANTIC_TOP_K,
) -> tuple[str, ...]:
    query_vector = embeddings[case.query]
    scored = sorted(
        (
            (cosine_similarity(query_vector, embeddings[memory.text]), memory.id)
            for memory in memories
        ),
        key=lambda item: (-item[0], item[1]),
    )
    return tuple(memory_id for score, memory_id in scored if score >= threshold)[:top_k]


def legacy_selection(
    memories: tuple[MemoryRecord, ...], limit: int = LEGACY_LIMIT
) -> tuple[str, ...]:
    newest = sorted(memories, key=lambda memory: (memory.created_at, memory.id), reverse=True)
    return tuple(memory.id for memory in newest[:limit])


def compute_metrics(
    cases: tuple[RecallCase, ...],
    semantic_by_case: dict[str, tuple[str, ...]],
    legacy_ids: tuple[str, ...],
) -> Metrics:
    matched = [case for case in cases if case.expected_memory_ids]
    no_match = [case for case in cases if not case.expected_memory_ids]
    if not matched or not no_match:
        raise ValueError("fixture must contain both matched and no-match cases")

    def recall(case: RecallCase, selected: tuple[str, ...]) -> float:
        expected = set(case.expected_memory_ids)
        return len(expected.intersection(selected)) / len(expected)

    semantic_recall = sum(recall(case, semantic_by_case[case.id]) for case in matched) / len(
        matched
    )
    legacy_recall = sum(recall(case, legacy_ids) for case in matched) / len(matched)
    false_selection = sum(bool(semantic_by_case[case.id]) for case in no_match) / len(no_match)
    return Metrics(
        semantic_recall_at_4=semantic_recall,
        legacy_recall_at_20=legacy_recall,
        improvement=semantic_recall - legacy_recall,
        no_match_false_selection_rate=false_selection,
        matched_cases=len(matched),
        no_match_cases=len(no_match),
    )


def run_evaluation(
    client: Any,
    fixture: Fixture,
    *,
    model: str,
    threshold: float,
) -> Metrics:
    texts = [memory.text for memory in fixture.memories]
    texts.extend(case.query for case in fixture.cases)
    embeddings = embed_batch(client, texts, model)
    semantic_by_case = {
        case.id: semantic_selection(case, fixture.memories, embeddings, threshold)
        for case in fixture.cases
    }
    return compute_metrics(fixture.cases, semantic_by_case, legacy_selection(fixture.memories))


def acceptance_failures(metrics: Metrics) -> tuple[str, ...]:
    failures: list[str] = []
    epsilon = 1e-12
    if metrics.semantic_recall_at_4 + epsilon < MIN_RECALL:
        failures.append("Recall@4")
    if metrics.improvement + epsilon < MIN_IMPROVEMENT:
        failures.append("legacy improvement")
    if metrics.no_match_false_selection_rate > MAX_NO_MATCH_FALSE_SELECTION + epsilon:
        failures.append("no-match false selection")
    return tuple(failures)


def exit_code_for(metrics: Metrics) -> int:
    return 1 if acceptance_failures(metrics) else 0


def _percent(value: float, *, signed: bool = False) -> str:
    return f"{value:+.1%}" if signed else f"{value:.1%}"


def format_report(metrics: Metrics, *, model: str, threshold: float) -> str:
    failures = acceptance_failures(metrics)
    verdict = "FAIL: " + ", ".join(failures) if failures else "PASS"
    return "\n".join(
        [
            f"offline memory recall heuristic: model={model} threshold={threshold:.2f} semantic_top_k=4",
            f"cases: matched={metrics.matched_cases} no_match={metrics.no_match_cases}",
            f"semantic Recall@4: {_percent(metrics.semantic_recall_at_4)} (>= 90.0%)",
            f"legacy latest20 recall: {_percent(metrics.legacy_recall_at_20)}",
            f"improvement vs legacy: {_percent(metrics.improvement, signed=True)} (>= +30.0%p)",
            "no-match false-selection rate: "
            f"{_percent(metrics.no_match_false_selection_rate)} (<= 5.0%)",
            verdict,
        ]
    )


def main(argv: list[str] | None = None, *, client_factory: Any = OpenAI) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    args = parser.parse_args(argv)

    if not settings.openai_api_key:
        print("memory recall eval requires OPENAI_API_KEY", file=sys.stderr)
        return 2
    try:
        fixture = load_fixture(args.fixture)
        client = client_factory(api_key=settings.openai_api_key)
        metrics = run_evaluation(
            client,
            fixture,
            model=EMBEDDING_MODEL,
            threshold=settings.memory_recall_search_threshold,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"memory recall eval failed: {exc}", file=sys.stderr)
        return 2
    print(
        format_report(
            metrics,
            model=EMBEDDING_MODEL,
            threshold=settings.memory_recall_search_threshold,
        )
    )
    return exit_code_for(metrics)


if __name__ == "__main__":
    raise SystemExit(main())
