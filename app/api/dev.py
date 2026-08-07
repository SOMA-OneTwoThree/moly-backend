"""격리된 local/development 전용 API — 배치·회상을 Swagger에서 손으로 검증한다.

⚠️ main.create_app()이 local/development + ENABLE_DEV_ROUTES일 때만 등록한다. 운영엔 라우트가 없다.
   일기 생성은 원래 배치 전용이라 API가 없다. 튜닝 루프를 돌리려면 두 벽을 넘어야 한다:
   - 멱등: 같은 (user, diary_date) 행이 있으면 조용히 스킵 → force로 지우고 재생성
   - 발행시각: published_at = 익일 09:00이라 조회 API에 안 뜸 → publish_now로 현재 시각으로
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

import asyncio
import uuid

from app.core import errors
from app.core.db import get_session
from app.core.security import get_current_user
from app.core.time_utils import activity_date_for
from app.config import settings
from app.models.diary import Diary
from app.models.profile import Profile
from app.schemas.dev import (
    ChatCompareRequest,
    ChatCompareResponse,
    ChatEvalRequest,
    DiaryGenerateRequest,
    DiaryGenerateResponse,
    EvalResultOut,
    DiaryRecallDiagnosticsResponse,
    MemoryRecallDiagnosticsResponse,
    RecallDiariesRequest,
    RecallMemoryRequest,
)
from app.services import diary_generation, model_eval
from app.services.account import _uid
from app.services.limits import effective_token_config
from app.services.prompts import system_prompt

router = APIRouter(tags=["dev"], prefix="/dev")


def _dev_operator_ids() -> set[uuid.UUID]:
    """설정 전체가 유효할 때만 operator allowlist를 연다.

    한 토큰이라도 UUID가 아니면 오타 난 설정으로 일부 사용자에게 비용·변형 route를 열지 않고
    전체 fail-closed한다.
    """

    raw = settings.dev_operator_user_ids.strip()
    if not raw:
        return set()
    try:
        return {uuid.UUID(value.strip()) for value in raw.split(",") if value.strip()}
    except ValueError:
        return set()


async def require_dev_operator(
    user_id: str = Depends(get_current_user),
) -> str:
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise errors.forbidden() from None
    if uid not in _dev_operator_ids():
        raise errors.forbidden()
    return user_id


@router.post("/diaries/generate", response_model=DiaryGenerateResponse)
async def generate_diary(
    req: DiaryGenerateRequest,
    user_id: str = Depends(require_dev_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """워커의 일기 생성 1건을 지금 실행하고, 결과 + 왜 그렇게 나왔는지를 돌려준다."""
    uid = _uid(user_id)
    profile = await session.get(Profile, uid)
    if profile is None:
        raise errors.AppError("NOT_FOUND", 404, "프로필을 찾을 수 없어요.")

    now = datetime.now(timezone.utc)
    target = req.target_date or activity_date_for(now, profile.timezone)

    if req.force:
        await session.execute(
            delete(Diary).where(
                Diary.user_id == uid,
                Diary.activity_date == target,
                Diary.kind.in_(("shared_day", "capi_day")),
            )
        )
        await session.execute(
            text("DELETE FROM diary_generation_results WHERE user_id=:user_id AND target_date=:day"),
            {"user_id": uid, "day": target},
        )
        await session.commit()

    cfg = await effective_token_config(session)
    diag = await diary_generation.generate_for_user(session, profile, target, cfg)

    diary = (
        await session.execute(
            select(Diary).where(
                Diary.user_id == uid,
                Diary.activity_date == target,
                Diary.kind.in_(("shared_day", "capi_day")),
            )
        )
    ).scalars().first()

    if diary is not None and req.publish_now:
        diary.published_at = now
        await session.commit()

    return {
        "target_date": target.isoformat(),
        # 왜 개인일기가 됐는지 / 왜 preset으로 빠졌는지
        "diagnostics": {
            **diag,
            "hint": _hint(diag),
        },
        "diary": None
        if diary is None
        else {
            "id": str(diary.id),
            "source": diary.source,  # llm = 개인일기 / preset = 캐피 자기일기
            "weather": diary.weather,
            "content": diary.content,
            "published_at": diary.published_at.isoformat() if diary.published_at else None,
        },
    }


# --- 대화 모델 A/B 평가(dev 전용) — 운영 chat 경로와 분리, 품질·속도·비용 실측 ---
def _system_for(req) -> str:  # noqa: ANN001
    return system_prompt(req.language) if req.use_persona else ""


def _messages(req) -> list[dict]:  # noqa: ANN001
    return [{"role": m.role, "content": m.content} for m in req.messages]


@router.post("/chat-eval", response_model=EvalResultOut)
async def chat_eval(
    req: ChatEvalRequest,
    _user_id: str = Depends(require_dev_operator),
) -> dict[str, Any]:
    """한 모델에 대화를 1회 투입 — 응답 텍스트 + 지연(ms) + 토큰 + 추정비용."""
    r = await model_eval.run_eval(
        req.provider, req.model, _system_for(req), _messages(req), max_tokens=req.max_tokens
    )
    return r.__dict__


@router.post("/chat-eval/compare", response_model=ChatCompareResponse)
async def chat_eval_compare(
    req: ChatCompareRequest,
    _user_id: str = Depends(require_dev_operator),
) -> dict[str, Any]:
    """같은 대화를 여러 모델에 동시 투입 — 나란히 비교(A/B). models 생략 시 기본 셋."""
    system = _system_for(req)
    msgs = _messages(req)
    targets = (
        [(m.provider, m.model) for m in req.models]
        if req.models
        else [(d["provider"], d["model"]) for d in model_eval.DEFAULT_MODELS]
    )
    results = await asyncio.gather(
        *(
            model_eval.run_eval(p, m, system, msgs, max_tokens=req.max_tokens)
            for p, m in targets
        )
    )
    return {"results": [r.__dict__ for r in results]}


@router.get("/memory/state")
async def inspect_memory_state(
    user_id: str = Depends(require_dev_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """v2 기억 파이프라인 상태 한 장(읽기 전용).

    감사 때 이 값들을 보려고 매번 SQL을 짜야 했다. registry·provenance·계약·관계·checkpoint를
    한 번에 본다 — 무엇이 **비어 있는지**가 특히 중요하다(없는 걸 안전으로 읽지 않도록).
    """
    uid = uuid.UUID(user_id)
    rows = (await session.execute(text("""
        SELECT
          (SELECT mode FROM memory_pipeline_states WHERE user_id=:u) AS mode,
          (SELECT bootstrap_status FROM memory_pipeline_states WHERE user_id=:u) AS bootstrap,
          (SELECT source_through_turn_seq FROM memory_pipeline_states WHERE user_id=:u) AS src,
          (SELECT ingest_through_turn_seq FROM memory_pipeline_states WHERE user_id=:u) AS ingest,
          (SELECT consolidated_through_turn_seq FROM memory_pipeline_states WHERE user_id=:u)
            AS consolidated,
          (SELECT count(*) FROM mem0_memory_registry WHERE user_id=:u
             AND semantic_status='active') AS active,
          (SELECT count(*) FROM mem0_memory_registry WHERE user_id=:u
             AND semantic_status='ambiguous') AS ambiguous,
          (SELECT count(*) FROM mem0_memory_registry WHERE user_id=:u
             AND semantic_status NOT IN ('active','ambiguous')) AS closed,
          (SELECT count(*) FROM mem0_memory_sources WHERE user_id=:u) AS provenance,
          (SELECT count(*) FROM user_interaction_contracts WHERE user_id=:u
             AND status='published') AS contracts_published,
          (SELECT count(*) FROM user_interaction_contracts WHERE user_id=:u
             AND status='draft') AS contracts_draft,
          (SELECT count(*) FROM relationship_profile_renders WHERE user_id=:u) AS rel_renders,
          (SELECT relationship_stage FROM user_relationship_states WHERE user_id=:u) AS stage,
          (SELECT count(*) FROM conversation_checkpoints WHERE user_id=:u
             AND publish_state='published') AS checkpoints_published,
          (SELECT count(*) FROM conversation_checkpoints WHERE user_id=:u
             AND publish_state='ready') AS checkpoints_ready
    """), {"u": uid})).mappings().first()
    return dict(rows or {})


@router.post("/recall/memory", response_model=MemoryRecallDiagnosticsResponse)
async def inspect_memory_recall(
    req: RecallMemoryRequest,
    user_id: str = Depends(require_dev_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """실제 챗과 **같은 경로**로 기억을 읽는다(읽기 전용).

    ⚠️ v2 사용자는 챗이 mem0 registry 검색을 쓰는데 이 진단만 legacy를 봤다. 그래서 여기서
    "기억이 있다/없다"를 확인해도 실제 대화 결과와 일치하지 않았다(감사 지적). 사용자 mode에
    따라 챗과 같은 코드를 탄다.
    """
    from app.services import chat, mem0_recall, memory_embeddings

    items = await mem0_recall.recall(
        session, uuid.UUID(user_id), query=req.query,
        adapter=chat._recall_adapter(),
        embed_query=memory_embeddings.embed_query,
    )
    return {"results": [{
        "text": i.text, "status": i.status, "distance": i.distance,
        "occurred_at": i.occurred_at.isoformat() if i.occurred_at else None,
        "uncertain": i.uncertain,
    } for i in items]}


@router.post("/recall/diaries", response_model=DiaryRecallDiagnosticsResponse)
async def inspect_diary_recall(
    req: RecallDiariesRequest,
    user_id: str = Depends(require_dev_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """실제 agent와 같은 일기 recall service를 현재 인증 사용자 범위에서 읽기 전용 실행한다."""
    from app.services import memory_embeddings, recall_diaries

    query_embedding = await memory_embeddings.embed_query(req.query) if req.query else None

    result = await recall_diaries.recall(
        session,
        user_id,
        query=req.query,
        need=req.need,
        from_date=req.from_date,
        to_date=req.to_date,
        focus_id=req.focus_id,
        limit=req.limit,
        query_embedding=query_embedding,
    )
    return {"capability": "recall_diaries", "result": result}


def _hint(diag: dict[str, Any]) -> str:
    if diag.get("skipped"):
        return "이미 일기가 있어 스킵됨. force=true로 재생성하세요."
    if diag.get("source") == "llm":
        return "개인일기 생성 성공."
    if diag.get("reason") == "no_scheduled_entry":
        return "지정된 캐피 일기가 없어 미발행으로 처리됨. 가짜 일기 행은 만들지 않음."
    if not diag.get("gate_passed"):
        return (
            f"게이트 미달 → preset 폴백. 당일 유저 메시지 {diag.get('user_chars')}자 "
            f"(필요 {diag.get('gate')}자). 대화를 더 하세요."
        )
    if diag.get("empty_body"):
        return "일기 LLM이 빈 본문을 반환 → preset 폴백."
    if diag.get("self_check_passed") is False:
        return "self-check가 경고했지만 비차단 정책으로 개인일기를 발행함."
    return "preset 폴백."
