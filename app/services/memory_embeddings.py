"""정규화 기억의 pgvector 임베딩 어댑터.

임베딩은 검색용 파생 데이터다. 사실의 진실 소스는 `memory_facts`이며, 재색인은
`relationship_profile_input_revision`을 올리지 않는다.
"""
from __future__ import annotations

from collections.abc import Sequence

from app.config import settings
from app.services import llm


async def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    if not texts:
        return []
    response = await llm._get_openai_client().embeddings.create(  # noqa: SLF001 — 공용 SDK client
        model=settings.embedder_model,
        input=list(texts),
        dimensions=settings.memory_embedding_dimensions,
        encoding_format="float",
        timeout=settings.llm_timeout_s,
    )
    ordered = sorted(response.data, key=lambda item: item.index)
    vectors = [list(item.embedding) for item in ordered]
    if len(vectors) != len(texts):
        raise RuntimeError(f"임베딩 응답 개수 불일치: expected={len(texts)} actual={len(vectors)}")
    invalid = [len(vector) for vector in vectors if len(vector) != settings.memory_embedding_dimensions]
    if invalid:
        raise RuntimeError(
            "임베딩 차원 불일치: "
            f"expected={settings.memory_embedding_dimensions} actual={invalid[0]}"
        )
    return vectors


async def embed_query(query: str) -> list[float]:
    return (await embed_texts([query]))[0]
