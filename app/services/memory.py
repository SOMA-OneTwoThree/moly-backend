"""장기기억(mem0) — 같은 Supabase pgvector. chat은 READ(주입)만, 쓰기는 워커 배치(04:00).

mem0 형식은 이 모듈에만 가둔다. user 연결 = metadata.user_id(FK 아님) → 탈퇴 시 delete_all 병행.
"""
from __future__ import annotations

import logging

from app.config import settings

_log = logging.getLogger("moly-backend")
_memory = None


def _get_memory():
    global _memory
    if _memory is None:
        from mem0 import AsyncMemory

        _memory = AsyncMemory.from_config(
            {
                "vector_store": {
                    "provider": "supabase",
                    "config": {
                        "connection_string": settings.supabase_db_connection_string,
                        "collection_name": settings.memory_collection,
                        "index_method": "hnsw",
                        "index_measure": "cosine_distance",
                    },
                },
                "llm": {
                    "provider": "openai",
                    "config": {
                        "api_key": settings.openai_api_key,
                        "model": settings.memory_llm_model,
                        "temperature": 0.1,
                    },
                },
                "embedder": {
                    "provider": "openai",
                    "config": {
                        "api_key": settings.openai_api_key,
                        "model": settings.embedder_model,
                    },
                },
            }
        )
    return _memory


def _render(items: list) -> str:
    """mem0 결과 → 프롬프트용 텍스트. recency 내림차순, 상한 컷."""
    parsed: list[tuple[str, str]] = []
    for it in items:
        if isinstance(it, str):
            parsed.append(("", it))
        elif isinstance(it, dict):
            content = it.get("memory") or it.get("content") or it.get("text") or ""
            if content:
                parsed.append((str(it.get("created_at") or ""), content))
    parsed.sort(key=lambda x: x[0], reverse=True)  # created_at desc(문자열 ISO 비교)
    top = parsed[: settings.memory_max_render_items]
    return "\n".join(f"- {content}" for _, content in top)


async def load_for_context(user_id: str) -> str:
    """유저 장기기억을 로드·랭킹·렌더. 미설정/오류면 빈 문자열(주입 생략, 대화는 계속)."""
    if not (settings.supabase_db_connection_string and settings.openai_api_key):
        return ""
    try:
        results = await _get_memory().get_all(
            filters={"user_id": user_id}, top_k=settings.memory_load_top_k
        )
    except Exception as e:  # noqa: BLE001  # 기억 로드 실패가 대화를 막지 않게
        _log.warning("기억 로드 실패(주입 생략): %r", e)
        return ""
    items = results.get("results", results) if isinstance(results, dict) else results
    return _render(items or [])


async def add_conversation(user_id: str, messages: list[dict]) -> None:
    """워커 배치용 — 그날 대화를 mem0에 추출·저장(chat 경로 아님)."""
    if messages:
        await _get_memory().add(messages, user_id=user_id)


async def delete_all(user_id: str) -> None:
    """탈퇴용 — mem0 기억 전량 삭제(FK 밖이라 CASCADE 안 됨, ERD §7)."""
    await _get_memory().delete_all(user_id=user_id)
