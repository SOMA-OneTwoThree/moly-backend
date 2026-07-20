"""장기기억(mem0) — 같은 Supabase pgvector. chat은 READ, 쓰기는 매시 워커 배치.

mem0 형식은 이 모듈에만 가둔다. user 연결 = metadata.user_id(FK 아님)이며 탈퇴 시 전용 RPC로 정리한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass

from app.config import settings

_log = logging.getLogger("moly-backend")
_memory = None
_memory_lock = threading.Lock()
_memory_init_lock = threading.Lock()
_memory_init_inflight = False
_memory_init_backoff_until = 0.0
_recall_failures = 0
_recall_backoff_until = 0.0
_recall_inflight = 0
_recall_gate_lock = threading.Lock()
_clock = time.monotonic
_RECALL_MAX_INFLIGHT = 3

MEMORY_EXTRACTION_INSTRUCTIONS = """
Extract only durable facts, preferences, relationships, plans, and events that the user directly
stated or explicitly confirmed. Assistant messages are context only: never store the assistant's
fictional experiences, advice, recommendations, inferred emotions, guesses, or acknowledgements
unless the user explicitly confirms them as their own fact or adopted plan. Every extracted memory
must be grounded in user evidence and attributed_to must be "user". If there is no qualifying user
fact, return an empty memory list. Preserve the user's language and do not translate the memory.
""".strip()


class MemoryUnavailable(Exception):
    """mem0 읽기 전이 장애 — 호출측이 스냅샷 폴백 또는 fail-open을 판단한다.

    '기억 없음'(빈 성공)과 반드시 구분해야 한다: 빈 성공에 스냅샷 재사용을 얹으면
    삭제된 기억이 부활한다(프라이버시). 그래서 실패는 raise, 빈 성공은 "" 반환.
    """


class MemoryWriteUnavailable(Exception):
    """mem0가 반환한 기억이 vector store에 실제 저장되지 않았다."""


def _normalize_extraction_response(response) -> str:
    if not isinstance(response, str) or not response.strip():
        raise MemoryWriteUnavailable("mem0 extraction returned an empty response")
    raw = response.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise MemoryWriteUnavailable("mem0 extraction returned invalid JSON") from None
        try:
            payload = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as exc:
            raise MemoryWriteUnavailable("mem0 extraction returned invalid JSON") from exc
    memories = payload.get("memory") if isinstance(payload, dict) else None
    if not isinstance(memories, list):
        raise MemoryWriteUnavailable("mem0 extraction omitted the memory list")
    safe: list[dict] = []
    for item in memories:
        if isinstance(item, dict) and item.get("attributed_to") == "assistant":
            continue
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("text"), str)
            or not item["text"].strip()
            or item.get("attributed_to") != "user"
        ):
            raise MemoryWriteUnavailable("mem0 extraction returned an unsafe memory")
        safe.append(item)
    payload["memory"] = safe
    return json.dumps(payload, ensure_ascii=False)


def _validate_extraction_response(response) -> None:
    _normalize_extraction_response(response)


class _StrictExtractionLLM:
    def __init__(self, delegate):
        self._delegate = delegate

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def generate_response(self, *args, **kwargs):
        response = self._delegate.generate_response(*args, **kwargs)
        if kwargs.get("response_format") == {"type": "json_object"}:
            return _normalize_extraction_response(response)
        return response


class _Mem0WriteFailureCapture(logging.Handler):
    _PREFIXES = (
        "Failed to embed memory text (async):",
        "Error parsing extraction response (async):",
        "Failed to insert memory ",
    )

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.failed = False

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith(self._PREFIXES):
            self.failed = True


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


def _create_memory():
    from mem0 import AsyncMemory

    client = AsyncMemory.from_config(
        {
            "history_db_path": ":memory:",
            "custom_instructions": MEMORY_EXTRACTION_INSTRUCTIONS,
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
                    "temperature": 0.0,
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
    for component in (getattr(client, "llm", None), getattr(client, "embedding_model", None)):
        provider_client = getattr(component, "client", None)
        with_options = getattr(provider_client, "with_options", None)
        if callable(with_options):
            component.client = with_options(
                timeout=settings.memory_provider_timeout_seconds,
                max_retries=settings.memory_provider_max_retries,
            )
    return client


def _get_memory():
    global _memory
    if _memory is None:
        with _memory_lock:
            if _memory is None:
                _memory = _create_memory()
    return _memory


def _prewarm_memory() -> None:
    global _memory_init_backoff_until, _memory_init_inflight
    try:
        _get_memory()
    except Exception as exc:  # noqa: BLE001
        with _memory_init_lock:
            _memory_init_backoff_until = (
                time.monotonic() + max(0, settings.memory_recall_backoff_seconds)
            )
        _log.warning("mem0 prewarm 실패: %s", type(exc).__name__)
    finally:
        with _memory_init_lock:
            _memory_init_inflight = False


def start_memory_prewarm() -> None:
    """mem0 초기화를 요청 지연·search breaker와 분리해 백그라운드에서 한 번만 수행한다."""
    global _memory_init_inflight
    if _memory is not None or not (
        settings.supabase_db_connection_string and settings.openai_api_key
    ):
        return
    with _memory_init_lock:
        if (
            _memory is not None
            or _memory_init_inflight
            or time.monotonic() < _memory_init_backoff_until
        ):
            return
        _memory_init_inflight = True
    threading.Thread(
        target=_prewarm_memory,
        name="mem0-prewarm",
        daemon=True,
    ).start()


def _ready_memory_for_recall():
    if _memory is None:
        start_memory_prewarm()
        raise MemoryUnavailable("semantic memory is warming")
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

    성공(빈 결과 포함) = 렌더값("" 가능).
    전이 장애 = MemoryUnavailable raise(빈 성공과 구분 — 호출측이 스냅샷 폴백 판단).
    """
    if not (settings.supabase_db_connection_string and settings.openai_api_key):
        raise MemoryUnavailable("legacy memory is not configured")
    try:
        # legacy는 기존 동작처럼 첫 로드도 완료해야 한다. 초기화 자체는 thread로 보내
        # event loop를 막지 않고, 보통은 앱 시작 prewarm이 먼저 끝낸다.
        client = await asyncio.to_thread(_get_memory)
        results = await client.get_all(
            filters={"user_id": user_id}, top_k=settings.memory_load_top_k
        )
    except MemoryUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        _log.warning("기억 로드 실패: %r", e)
        raise MemoryUnavailable(str(e)) from e
    items = results.get("results", results) if isinstance(results, dict) else results
    return _render(items or [])


@dataclass(frozen=True)
class RecalledMemory:
    memory_id: str
    text: str
    score: float
    observed_at: str


def _search_items(results) -> list:
    items = results.get("results", results) if isinstance(results, dict) else results
    return list(items or [])


def _select_search_results(items: list) -> list[RecalledMemory]:
    """검색 점수로 선발한 뒤 모델에는 관찰일 오름차순으로 제공한다."""
    candidates: list[tuple[float, int, str, str, str, str]] = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            raw_text, memory_id, score, observed_at, metadata = item, "", 0.0, "", {}
            attributed_to = None
        elif isinstance(item, dict):
            raw_text = item.get("memory") or item.get("content") or item.get("text") or ""
            memory_id = str(item.get("id") or "")
            try:
                score = float(item.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            attributed_to = item.get("attributed_to", metadata.get("attributed_to"))
            observed_at = str(
                metadata.get("activity_date")
                or item.get("activity_date")
                or item.get("updated_at")
                or item.get("created_at")
                or ""
            )
        else:
            continue
        if isinstance(attributed_to, str) and attributed_to.strip().casefold() == "assistant":
            continue
        normalized = _sanitize(str(raw_text))
        dedupe_key = " ".join(normalized.casefold().split())
        if not dedupe_key:
            continue
        candidates.append((score, index, normalized, dedupe_key, memory_id, observed_at))

    candidates.sort(key=lambda row: (-row[0], row[1]))
    selected: list[RecalledMemory] = []
    seen: set[str] = set()
    remaining = max(0, settings.memory_recall_max_chars)
    for score, _index, text, dedupe_key, memory_id, observed_at in candidates:
        if len(selected) >= max(0, settings.memory_recall_max_items) or remaining == 0:
            break
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        bounded = text[:remaining].rstrip()
        if not bounded:
            continue
        selected.append(RecalledMemory(memory_id, bounded, score, observed_at))
        remaining -= len(bounded)
    selected.sort(key=lambda item: item.observed_at)
    return selected


def _record_recall_failure(now: float) -> None:
    global _recall_backoff_until, _recall_failures
    _recall_failures += 1
    if _recall_failures >= max(1, settings.memory_recall_backoff_failures):
        _recall_failures = 0
        _recall_backoff_until = now + max(0, settings.memory_recall_backoff_seconds)


def _try_acquire_recall_slot() -> bool:
    global _recall_inflight
    with _recall_gate_lock:
        if _recall_inflight >= _RECALL_MAX_INFLIGHT:
            return False
        _recall_inflight += 1
        return True


def _release_recall_slot(task: asyncio.Task | None = None) -> None:
    global _recall_inflight
    if task is not None:
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
    with _recall_gate_lock:
        _recall_inflight = max(0, _recall_inflight - 1)


async def search_for_context(user_id: str, query: str) -> list[RecalledMemory]:
    """user 범위 의미 검색. 실패와 backoff는 빈 결과와 구분해 호출측이 fail-open한다."""
    global _recall_failures
    if not query:
        return []
    if not (settings.supabase_db_connection_string and settings.openai_api_key):
        raise MemoryUnavailable("semantic memory is not configured")
    now = _clock()
    if now < _recall_backoff_until:
        raise MemoryUnavailable("semantic recall backoff active")
    client = _ready_memory_for_recall()
    if not _try_acquire_recall_slot():
        raise MemoryUnavailable("semantic recall busy")
    task = asyncio.create_task(
        client.search(
            query,
            filters={"user_id": user_id},
            top_k=settings.memory_recall_search_top_k,
            threshold=settings.memory_recall_search_threshold,
            rerank=False,
        )
    )
    try:
        results = await asyncio.wait_for(
            asyncio.shield(task),
            timeout=max(1, settings.memory_recall_timeout_ms) / 1000,
        )
    except asyncio.CancelledError:
        task.add_done_callback(_release_recall_slot)
        raise
    except Exception as exc:  # noqa: BLE001
        if task.done():
            _release_recall_slot(task)
        else:
            task.add_done_callback(_release_recall_slot)
        _record_recall_failure(now)
        raise MemoryUnavailable("semantic memory search unavailable") from exc
    _release_recall_slot()
    _recall_failures = 0
    return _select_search_results(_search_items(results))


async def add_conversation(
    user_id: str, messages: list[dict], *, metadata: dict | None = None
) -> int:
    """워커 배치용 — 그날 대화를 mem0에 추출·저장(chat 경로 아님)."""
    if not messages:
        return 0
    prompt = MEMORY_EXTRACTION_INSTRUCTIONS
    if metadata and metadata.get("activity_date"):
        prompt += (
            "\nTreat " + str(metadata["activity_date"])
            + " as the observation date for resolving relative dates."
        )
    client = await asyncio.to_thread(_get_memory)
    mem0_logger = logging.getLogger("mem0.memory.main")
    failure_capture = _Mem0WriteFailureCapture()
    original_llm = getattr(client, "llm", None)
    if original_llm is not None:
        client.llm = _StrictExtractionLLM(original_llm)
    mem0_logger.addHandler(failure_capture)
    try:
        result = await client.add(
            messages,
            user_id=user_id,
            metadata=metadata,
            prompt=prompt,
        )
    finally:
        mem0_logger.removeHandler(failure_capture)
        if original_llm is not None:
            client.llm = original_llm
    if failure_capture.failed:
        raise MemoryWriteUnavailable("mem0 swallowed a memory write failure")
    items = _search_items(result)
    for item in items:
        memory_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(memory_id, str) or not memory_id:
            raise MemoryWriteUnavailable("mem0 returned a memory without an id")
        persisted = await client.get(memory_id)
        if not isinstance(persisted, dict) or persisted.get("user_id") != user_id:
            raise MemoryWriteUnavailable(f"mem0 memory {memory_id} was not persisted")
    return len(items)


async def delete_all(user_id: str) -> None:
    """탈퇴용 — mem0 기억 전량 삭제(FK 밖이라 CASCADE 안 됨, ERD §7)."""
    client = await asyncio.to_thread(_get_memory)
    await client.delete_all(user_id=user_id)


async def sweep_orphans(session) -> int:
    """탈퇴 고아 기억 청소(백스톱). vecs 컬렉션은 profiles CASCADE가 안 닿는다.

    user_id는 top-level 컬럼이 아니라 metadata jsonb 안(실 스키마 확인).
    기억 쓰기는 profiles FK가 있는 ingestion state에서만 시작하므로 profile 없는 기억은
    정상 생성 중인 데이터일 수 없다. timestamp가 없거나 깨진 legacy 행도 즉시 정리한다.
    UUID가 아닌 외부 데이터는 건너뛰며 RPC가 두 컬렉션을 행 제한 없이 원자적으로 삭제한다.
    즉시 삭제(탈퇴)는 moly-auth 계약(별건) — 이건 늦게라도 지우는 안전망.
    """
    from sqlalchemy import text as _text

    existence = await session.execute(
        _text(
            "SELECT to_regclass('vecs.memories') IS NOT NULL, "
            "       to_regclass('vecs.memories_entities') IS NOT NULL"
        )
    )
    has_memories, has_entities = existence.one()
    if not (has_memories or has_entities):
        await session.commit()
        return 0

    candidates: list[str] = []
    if has_memories:
        candidates.append(
            "SELECT DISTINCT m.metadata->>'user_id' AS user_id "
            "FROM vecs.memories m "
            "WHERE m.metadata->>'user_id' IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM public.profiles p "
            "                WHERE p.id::text = m.metadata->>'user_id')"
        )
    if has_entities:
        candidates.append(
            "SELECT DISTINCT e.metadata->>'user_id' AS user_id "
            "FROM vecs.memories_entities e "
            "WHERE e.metadata->>'user_id' IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM public.profiles p "
            "                WHERE p.id::text = e.metadata->>'user_id')"
        )

    rows = await session.execute(_text(" UNION ".join(candidates)))
    deleted = 0
    for raw_user_id in rows.scalars().all():
        try:
            user_id = str(uuid.UUID(raw_user_id))
        except (TypeError, ValueError, AttributeError):
            _log.warning("UUID가 아닌 고아 기억 user_id 건너뜀: %r", raw_user_id)
            continue
        result = await session.execute(
            _text("SELECT public.delete_memory_artifacts(CAST(:user_id AS uuid))"),
            {"user_id": user_id},
        )
        deleted += result.scalar_one() or 0

    await session.commit()
    return deleted
