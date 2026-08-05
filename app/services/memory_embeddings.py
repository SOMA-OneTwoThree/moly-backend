"""정규화 기억의 pgvector 임베딩 어댑터.

임베딩은 검색용 파생 데이터다. 사실의 진실 소스는 `memory_facts`이며, 재색인은
`relationship_profile_input_revision`을 올리지 않는다.
"""
from __future__ import annotations

from collections.abc import Sequence

from app.config import settings
from app.services import llm


async def embed_texts(
    texts: Sequence[str], *, timeout: float | None = None
) -> list[list[float]]:
    if not texts:
        return []
    response = await llm._get_openai_client().embeddings.create(  # noqa: SLF001 — 공용 SDK client
        model=settings.embedder_model,
        input=list(texts),
        dimensions=settings.memory_embedding_dimensions,
        encoding_format="float",
        # ⚠️ 호출측 예산을 받는다. 기본(60초)만 쓰면 챗의 5초 마감 안에서 부르는 쪽이
        #    자기 예산을 강제할 수 없다 — 회상이 60초를 기다리게 된다(감사 지적).
        timeout=settings.llm_timeout_s if timeout is None else timeout,
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


async def embed_query(query: str, *, timeout: float | None = None) -> list[float]:
    return (await embed_texts([query], timeout=timeout))[0]
