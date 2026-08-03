"""장기기억 **façade** — legacy(mem0)와 normalized(memory_facts, W8)를 한 인터페이스 뒤에 둔다.

mem0 형식은 이 모듈에만 가둔다. user 연결 = metadata.user_id(FK 아님) → 탈퇴 시 delete_all 병행.
chat은 READ(주입)만, mem0 쓰기는 워커 배치(04:00).

**mode 분기**(W8): 유저별 기억 방식은 `chat_contexts.memory_mode`가 가른다.
- `legacy` — 지금까지의 mem0 경로. 기본값이고 **동작이 하나도 바뀌지 않는다**.
- `normalized` — 구조화 사실(`memory_facts`) 경로. 주입은 W9 관계 프로필이, 실제 전환(cutover)은
  W10이 한다. 그래서 여기서는 **mem0를 쓰지도 읽지도 않는 것**까지만 보장한다: 빈 기억으로
  fail-open하고 legacy 문자열로 폴백하지 않는다(폴백하면 잊은 내용이 되살아난다).

`mode` 인자는 기본값이 `legacy`라 기존 호출부는 그대로 동작한다. cutover 때 호출부가
`chat_contexts.memory_mode`를 읽어 넘긴다(`memory_repo.resolve_mode`).
"""
from __future__ import annotations

import asyncio
import logging
import os
import unicodedata

from app.config import settings
from app.services import i18n

# mem0 쓰기 직렬화 — mem0 히스토리는 SQLite 파일이라 동시 쓰기 시 'database is locked'가 난다.
# 워커 동시성을 올려도(SOMA-349 Phase 2) mem0 add만은 한 번에 하나씩 흘려보낸다. best-effort라
# 이 병목은 허용 범위(일기 생성이 진짜 병목). 동시성=1이면 사실상 무비용.
_WRITE_LOCK = asyncio.Semaphore(1)

# mem0 add는 대화 전체(role 프리픽스 포함 직렬화)를 하나의 임베딩 입력으로 OpenAI에 보낸다.
# text-embedding-3-small 입력 상한 = 8191 토큰. 헤비 유저(수다쟁이) 대화가 이를 넘으면 400으로
# add 전체가 실패해 그날 기억이 유실된다(SOMA-385). → 배치로 나눠 여러 번 add한다.
# 상한은 '문자수'로 잡는다(tiktoken 미의존 — 봉인 컨테이너 다운로드 실패 회피). 한국어 최악
# ~3토큰/char 가정해도 2500자면 ~7500토큰 < 8191(안전마진). mem0가 배치 간 사실을 의미기반
# dedup·링크하므로(additive 파이프라인) 나눠 넣어도 최종 기억은 온전하다.
_EMBED_MAX_CHARS = 2500
_MSG_OVERHEAD_CHARS = 12  # "assistant: "(11) + 개행 상한(직렬화 형태로 카운트, "user: "는 과대추정=안전)


def _chunk_messages(messages: list[dict]) -> list[list[dict]]:
    """메시지를 임베딩 상한 이하 배치로 청킹(메시지 경계). 단일 초장문 메시지는 내용을 무손실 분할."""
    batches: list[list[dict]] = []
    cur: list[dict] = []
    cur_len = 0
    for m in messages:
        content = m.get("content") or ""
        mlen = len(content) + _MSG_OVERHEAD_CHARS
        if mlen > _EMBED_MAX_CHARS:  # 단일 메시지가 상한 초과 → 내용을 조각내 각자 배치로(무손실)
            if cur:
                batches.append(cur)
                cur, cur_len = [], 0
            step = _EMBED_MAX_CHARS - _MSG_OVERHEAD_CHARS
            for i in range(0, len(content), step):
                batches.append([{**m, "content": content[i : i + step]}])
            continue
        if cur and cur_len + mlen > _EMBED_MAX_CHARS:
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(m)
        cur_len += mlen
    if cur:
        batches.append(cur)
    return batches

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


# 정규화 기억(W8)이 승계하는 같은 살균 규칙. 프롬프트에 들어가는 기억 문자열은 legacy든
# normalized든 같은 방어선을 통과해야 하므로 규칙을 복제하지 않고 이 이름으로 공유한다.
sanitize_text = _sanitize

# chat_contexts.memory_mode 값(= DB CHECK 값). 문자열 리터럴을 여기저기 흩지 않는다.
MODE_LEGACY = "legacy"
MODE_NORMALIZED = "normalized"


# mem0 fact 추출 지시(최우선 규칙) — 유저 언어로 저장 + 이름/호칭 미저장(개명 드리프트·프라이버시 방어).
# en/ja 유저에게 한국어 지시를 주면 (1)외국어 원문을 한국어로 번역·왜곡 저장(언어 불일치)
# (2)지시(한국어) vs 데이터(외국어) 충돌로 이름미저장 준수율 저하 → 언어별로 분기한다(SOMA-365).
_MEMORY_INSTRUCTIONS = {
    "ko": (
        "- 모든 기억은 반드시 한국어로 간결하게 작성한다.\n"
        "- 사용자를 지칭할 때 실제 이름·닉네임·호칭을 쓰지 말고 '사용자'로만 표현한다. 이름 자체는 저장하지 않는다.\n"
        "- 감정·관계·고민·취향·상황 등 사람을 이해하는 데 중요한 사실 위주로 뽑는다."
    ),
    "en": (
        "- Write every memory concisely in English.\n"
        "- Never store the user's real name, nickname, or honorific; refer to them only as 'the user'. "
        "Do not save the name itself.\n"
        "- Focus on facts that help understand the person: emotions, relationships, worries, tastes, situations."
    ),
    "ja": (
        "- すべての記憶は必ず日本語で簡潔に書く。\n"
        "- ユーザーを指すときは実名・ニックネーム・呼称を使わず「ユーザー」とだけ表現する。名前自体は保存しない。\n"
        "- 感情・関係・悩み・好み・状況など、その人を理解するのに重要な事実を中心に抽出する。"
    ),
}


def _instructions_for(language: str | None) -> str:
    """유저 언어 버킷의 mem0 추출 지시. 미설정(None)=ko, 지원 밖 코드(zh 등)=en(i18n.resolve)."""
    return _MEMORY_INSTRUCTIONS[i18n.resolve(language)]


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
                "custom_instructions": _MEMORY_INSTRUCTIONS["ko"],  # 기본값(add마다 prompt로 언어별 오버라이드)
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


async def load_for_context(user_id: str, *, mode: str = MODE_LEGACY) -> str:
    """유저 장기기억을 로드·랭킹·렌더.

    미설정 = "" (기능 OFF, 대화 계속). 성공(빈 결과 포함) = 렌더값("" 가능).
    전이 장애 = MemoryUnavailable raise(빈 성공과 구분 — 호출측이 스냅샷 폴백 판단).

    `normalized` 유저는 mem0를 **읽지 않는다**. 정규화 기억의 주입 경로는 관계 프로필(W9)이고,
    그게 없다고 mem0로 폴백하면 forget으로 지운 내용이 되살아난다 → 빈 기억으로 fail-open.
    """
    if mode == MODE_NORMALIZED:
        return ""
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


async def add_conversation(
    user_id: str, messages: list[dict], language: str | None = None, *, mode: str = MODE_LEGACY
) -> None:
    """워커 배치용 — 그날 대화를 mem0에 추출·저장(chat 경로 아님). SQLite 히스토리 락 회피 위해 직렬.

    `normalized` 유저는 no-op이다. 추출은 턴 단위 잡(`memory_extract`)이 이미 하고 있으므로,
    여기서 mem0에 또 쓰면 두 벌의 기억이 갈라진다.

    유저 언어로 추출한다(SOMA-365) — 전역 싱글턴은 유지하고 add마다 prompt로 언어 지시를 오버라이드
    (mem0 add의 prompt 인자가 custom_instructions보다 우선). en/ja 기억이 한국어로 번역 저장되던 문제 방지.

    임베딩 8192토큰 상한을 넘지 않도록 배치로 청킹해 저장한다(SOMA-385). 배치별 best-effort —
    한 배치가 실패해도 나머지는 계속 저장하고, 하나라도 실패하면 마지막에 raise(호출측이 memory_failed로
    관측). _WRITE_LOCK은 전 배치에 걸쳐 유지(배치 간 dedup search 일관성). 헤비 유저는 추출 LLM
    호출이 배치 수만큼(2~3배) 늘고 직렬 구간도 그만큼 길어진다.
    """
    if mode == MODE_NORMALIZED or not messages:
        return
    batches = _chunk_messages(messages)
    instr = _instructions_for(language)
    mem = _get_memory()
    last_err: Exception | None = None
    async with _WRITE_LOCK:
        for idx, batch in enumerate(batches):
            try:
                await mem.add(batch, user_id=user_id, prompt=instr)
            except Exception as e:  # noqa: BLE001  # 배치별 best-effort — 실패해도 나머지 진행
                last_err = e
                _log.warning("mem0 add 배치 %d/%d 실패(계속): %r", idx + 1, len(batches), e)
    if last_err is not None:
        raise last_err


async def delete_all(user_id: str) -> None:
    """탈퇴용 — mem0 기억 전량 삭제(FK 밖이라 CASCADE 안 됨, ERD §7).

    mode로 분기하지 않는다: normalized로 옮긴 유저도 전환 이전의 mem0 잔여분을 갖고 있으므로
    지우지 않으면 탈퇴 후에도 남는다(정규화 테이블 쪽은 profiles CASCADE가 처리한다).
    """
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
