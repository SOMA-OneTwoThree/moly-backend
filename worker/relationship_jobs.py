"""관계 상태 투영 잡 (7장).

event를 집계해 state를 갱신하고 locale render를 만든다. **챗 경로에서 집계하지 않는다** —
매 턴 전체 event를 세면 대화 지연이 사용자 이력 길이에 비례해 늘어난다.
"""
from __future__ import annotations

from sqlalchemy import text

from app.core.db import get_sessionmaker
from app.services import relationship_projector
from app.services.jobs import ClaimedJob
from worker import consumer
from worker.consumer import JobFatal, JobResult

JOB_RELATIONSHIP_PROJECT = "relationship_project"

_LANG = text("SELECT language FROM profiles WHERE id = :user_id")


async def handle_relationship_project(job: ClaimedJob) -> JobResult:
    if job.user_id is None:
        raise JobFatal("missing_user")
    uid = job.user_id

    async with get_sessionmaker()() as session:
        language = await session.scalar(_LANG, {"user_id": uid})

    async def _apply(session) -> None:
        await relationship_projector.project(session, uid, language=language)

    return JobResult(result_code="ok", apply_domain=_apply)


consumer.register(JOB_RELATIONSHIP_PROJECT, handle_relationship_project)
