"""장기기억(mem0) — 같은 Supabase pgvector. chat은 READ(주입)만, 쓰기는 워커 배치(04:00).

mem0 형식은 이 모듈에만 가둔다. user 연결 = metadata.user_id(FK 아님) → 탈퇴 시 delete_all 병행.
"""
from __future__ import annotations

import asyncio
import logging
import os
import unicodedata

from app.config import settings

# mem0 쓰기 직렬화 — mem0 히스토리는 SQLite 파일이라 동시 쓰기 시 'database is locked'가 난다.
# 워커 동시성을 올려도(SOMA-349 Phase 2) mem0 add만은 한 번에 하나씩 흘려보낸다. best-effort라
# 이 병목은 허용 범위(일기 생성이 진짜 병목). 동시성=1이면 사실상 무비용.
_WRITE_LOCK = asyncio.Semaphore(1)

# mem0는 ~/.mem0에 히스토리 SQLite·telemetry를 쓴다. 컨테이너 홈이 비쓰기면 PermissionError(13)로
# add가 매번 터져 기억이 조용히 전멸한다(2026-07 프로덕션 사고). 쓰기 가능 경로 강제 + telemetry off.
os.environ.setdefault("MEM0_DIR", "/tmp/mem0")
os.environ.setdefault("MEM0_TELEMETRY", "False")
_MEM0_DIR = os.environ["MEM0_DIR"]

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


# mem0 fact 추출 지시(최우선 규칙). 한국어 고정 + 이름/호칭 미저장(개명 드리프트·프라이버시 방어).
_MEMORY_INSTRUCTIONS = (
    "- 모든 기억은 반드시 한국어로 간결하게 작성한다.\n"
    "- 사용자를 지칭할 때 실제 이름·닉네임·호칭을 쓰지 말고 '사용자'로만 표현한다. 이름 자체는 저장하지 않는다.\n"
    "- 감정·관계·고민·취향·상황 등 사람을 이해하는 데 중요한 사실 위주로 뽑는다."
)


def _get_memory():
    global _memory
    if _memory is None:
        from mem0 import AsyncMemory

        os.makedirs(_MEM0_DIR, exist_ok=True)
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
                "custom_instructions": _MEMORY_INSTRUCTIONS,
                "history_db_path": os.path.join(_MEM0_DIR, "history.db"),
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
    """워커 배치용 — 그날 대화를 mem0에 추출·저장(chat 경로 아님). SQLite 히스토리 락 회피 위해 직렬."""
    if messages:
        async with _WRITE_LOCK:
            await _get_memory().add(messages, user_id=user_id)


async def delete_all(user_id: str) -> None:
    """탈퇴용 — mem0 기억 전량 삭제(FK 밖이라 CASCADE 안 됨, ERD §7)."""
    await _get_memory().delete_all(user_id=user_id)


async def sweep_orphans(session) -> int:
    """탈퇴 고아 기억 청소(백스톱). vecs.memories는 FK 밖이라 profiles CASCADE가 안 닿는다.

    created_at·user_id는 top-level 컬럼이 아니라 metadata jsonb 안(실 스키마 확인).
    NOT EXISTS(NOT IN NULL 트랩 회피) + profiles.id::text 캐스트. 유예로 온보딩 레이스 방어.
    즉시 삭제(탈퇴)는 moly-auth 계약(별건) — 이건 늦게라도 지우는 안전망.
    """
    from sqlalchemy import text as _text

    coll = settings.memory_collection.replace('"', "")
    grace = int(settings.memory_orphan_grace_hours)
    sql = _text(
        f'DELETE FROM vecs."{coll}" m '  # noqa: S608 (coll=config 상수, 따옴표 제거)
        "WHERE (m.metadata->>'created_at')::timestamptz < now() - make_interval(hours => :g) "
        "AND NOT EXISTS (SELECT 1 FROM public.profiles p "
        "                WHERE p.id::text = m.metadata->>'user_id')"
    )
    res = await session.execute(sql, {"g": grace})
    await session.commit()
    return res.rowcount or 0
