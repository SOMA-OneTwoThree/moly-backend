"""OpenAI embedding 어댑터의 순서·차원 계약."""
from types import SimpleNamespace

import pytest

from app.services import memory_embeddings


async def test_embedding_response_is_reordered_by_input_index(monkeypatch):
    dim = memory_embeddings.settings.memory_embedding_dimensions

    class Embeddings:
        async def create(self, **kwargs):
            return SimpleNamespace(data=[
                SimpleNamespace(index=1, embedding=[2.0] * dim),
                SimpleNamespace(index=0, embedding=[1.0] * dim),
            ])

    client = SimpleNamespace(embeddings=Embeddings())
    monkeypatch.setattr(memory_embeddings.llm, "_get_openai_client", lambda: client)
    vectors = await memory_embeddings.embed_texts(["a", "b"])
    assert vectors[0][0] == 1.0 and vectors[1][0] == 2.0


async def test_embedding_dimension_mismatch_fails_closed(monkeypatch):
    class Embeddings:
        async def create(self, **kwargs):
            return SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[1.0, 2.0])])

    client = SimpleNamespace(embeddings=Embeddings())
    monkeypatch.setattr(memory_embeddings.llm, "_get_openai_client", lambda: client)
    with pytest.raises(RuntimeError, match="차원 불일치"):
        await memory_embeddings.embed_texts(["a"])
