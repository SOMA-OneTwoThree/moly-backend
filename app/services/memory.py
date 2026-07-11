"""장기기억(mem0) — 같은 Supabase pgvector. chat은 READ(주입)만, 쓰기는 워커 배치(04:00).

mem0 형식은 이 모듈에만 가둔다. user 연결 = metadata.user_id(FK 아님) → 탈퇴 시 delete_all 병행.
"""
from __future__ import annotations

import logging
import unicodedata

from app.config import settings

_log = logging.getLogger("moly-backend")
_memory = None


class MemoryUnavailable(Exception):
    """mem0 로드 전이 장애 — 호출측이 스냅샷 폴백을 판단하게 한다.

    '기억 없음'(빈 성공)과 반드시 구분해야 한다: 빈 성공에 스냅샷 재사용을 얹으면
    삭제된 기억이 부활한다(프라이버시). 그래서 실패는 raise, 빈 성공은 "" 반환.
    """


# [기억]에 렌더되는 텍스트는 유저 대화에서 추출된 값 → 무살균 시 시스템 프롬프트 인젝션 통로.
# NFKC 정규화 후 제어문자·ZWSP/bidi·대괄호류를 제거해 가짜 섹션헤더([규칙] 등)·델리미터 위조를 막는다.
# (렌더 경로 전용 — 저장된 mem0 원본은 건드리지 않음. 일기는 messages 원문을 읽으므로 영향 없음.)
_STRIP = dict.fromkeys(
    list(range(0x00, 0x20))          # C0 제어문자(개행·탭 포함 → 한 기억이 여러 줄로 분리되는 것 방지)
    + list(range(0x7F, 0xA0))        # DEL + C1
    + [0x200B, 0x200C, 0x200D, 0x200E, 0x200F,  # ZWSP/ZWNJ/ZWJ/LRM/RLM
       0x202A, 0x202B, 0x202C, 0x202D, 0x202E,  # bidi embedding/override
       0x2066, 0x2067, 0x2068, 0x2069,          # bidi isolate
       0xFEFF],
    None,
)
_BRACKETS = {ord(c): None for c in "<>[]＜＞［］〈〉【】〈〉"}


def _sanitize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)  # 전각 대괄호 → ascii → 아래서 제거
    return text.translate(_STRIP).translate(_BRACKETS).strip()


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
                parsed.append((str(it.get("created_at") or ""), str(content)))
    parsed.sort(key=lambda x: x[0], reverse=True)  # created_at desc(문자열 ISO 비교)
    top = parsed[: settings.memory_max_render_items]
    lines = [f"- {s}" for _, c in top if (s := _sanitize(c))]  # 살균 후 빈 항목 제외
    return "\n".join(lines)


async def load_for_context(user_id: str) -> str:
    """유저 장기기억을 로드·랭킹·렌더.

    미설정 = "" (기능 OFF, 대화 계속). 성공(빈 결과 포함) = 렌더값("" 가능).
    전이 장애 = MemoryUnavailable raise(빈 성공과 구분 — 호출측이 스냅샷 폴백 판단).
    """
    if not (settings.supabase_db_connection_string and settings.openai_api_key):
        return ""
    try:
        results = await _get_memory().get_all(
            filters={"user_id": user_id}, top_k=settings.memory_load_top_k
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("기억 로드 실패: %r", e)
        raise MemoryUnavailable(str(e)) from e
    items = results.get("results", results) if isinstance(results, dict) else results
    return _render(items or [])


async def add_conversation(user_id: str, messages: list[dict]) -> None:
    """워커 배치용 — 그날 대화를 mem0에 추출·저장(chat 경로 아님)."""
    if messages:
        await _get_memory().add(messages, user_id=user_id)


async def delete_all(user_id: str) -> None:
    """탈퇴용 — mem0 기억 전량 삭제(FK 밖이라 CASCADE 안 됨, ERD §7)."""
    await _get_memory().delete_all(user_id=user_id)
