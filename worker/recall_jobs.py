"""Embedding handlers for episodic and diary recall projections."""
from __future__ import annotations

import uuid

from app.core.db import get_sessionmaker
from app.services import diary_recall_repo, episodic_memory, memory_embeddings
from app.services.jobs import ClaimedJob
from worker.consumer import JobCancelled, JobFatal, JobResult, JobRetry, register


def _owned(job: ClaimedJob) -> uuid.UUID:
    if job.user_id is None:
        raise JobFatal("missing_user")
    return job.user_id


async def handle_episode_embed(job: ClaimedJob) -> JobResult:
    user_id = _owned(job)
    try:
        message_id = int(job.payload["message_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise JobFatal("invalid_payload") from exc
    async with get_sessionmaker()() as session:
        source = await episodic_memory.load_for_embedding(
            session, user_id=user_id, message_id=message_id
        )
    if source is None:
        raise JobCancelled("suppressed_or_missing")
    content, content_hash = source
    try:
        vector = await memory_embeddings.embed_query(content)
    except Exception as exc:  # provider timeout/429/network
        raise JobRetry("embedding_failed") from exc

    async def apply(session) -> None:  # noqa: ANN001
        await episodic_memory.write_embedding(
            session,
            user_id=user_id,
            message_id=message_id,
            content_hash=content_hash,
            vector=vector,
        )

    return JobResult(result_code="indexed", apply_domain=apply)


async def handle_diary_embed(job: ClaimedJob) -> JobResult:
    user_id = _owned(job)
    try:
        diary_id = uuid.UUID(str(job.payload["diary_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise JobFatal("invalid_payload") from exc
    async with get_sessionmaker()() as session:
        source = await diary_recall_repo.embed_diary_document(
            session, user_id=user_id, diary_id=diary_id
        )
    if source is None:
        raise JobCancelled("suppressed_or_missing")
    body, generation, source_hash = source
    try:
        vector = await diary_recall_repo.embed_text(body)
    except Exception as exc:
        raise JobRetry("embedding_failed") from exc

    async def apply(session) -> None:  # noqa: ANN001
        await diary_recall_repo.write_diary_embedding(
            session,
            user_id=user_id,
            diary_id=diary_id,
            source_hash=source_hash,
            generation=generation,
            vector=vector,
        )

    return JobResult(result_code="indexed", apply_domain=apply)


register(episodic_memory.JOB_EPISODE_EMBED, handle_episode_embed)
register(diary_recall_repo.JOB_DIARY_RECALL_EMBED, handle_diary_embed)
