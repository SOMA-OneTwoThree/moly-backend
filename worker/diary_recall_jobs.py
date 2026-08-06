"""일기 회상 검색용 임베딩 생성 잡.

일기가 발행되거나 본문이 바뀌면 `diary_recall_repo.upsert_diary_recall_document`가 이 잡을
등록한다. 하는 일은 일기 본문을 임베딩해 `diary_recall_documents.embedding`에 넣는 것뿐이다.

**이 값이 비어 있으면 `recall_diaries` 도구의 유사도 검색이 아예 동작하지 않는다.** 그때는
`search_text ILIKE` 부분일치만 남아서, 사용자가 단어를 정확히 겹치게 말하지 않으면 일기를
못 찾는다. 실제로 등록만 하고 처리기가 없던 기간에는 이 잡이 집어 가는 즉시
`dead(unknown_job_type)`로 죽어서 임베딩이 한 건도 만들어지지 않았다.

순서는 외부 호출 동안 DB를 쥐지 않는다는 잡 플랫폼 규칙을 따른다.
읽기(짧은 세션) → 세션 반납 → 임베딩(외부 호출) → 확정 트랜잭션에서 쓰기.
"""
from __future__ import annotations

import logging
import uuid

from app.core.db import get_sessionmaker
from app.services import diary_recall_repo
from app.services.jobs import ClaimedJob
from worker import consumer
from worker.consumer import JobFatal, JobResult, JobRetry

_log = logging.getLogger("moly-worker")

JOB_DIARY_RECALL_EMBED = diary_recall_repo.JOB_DIARY_RECALL_EMBED


async def handle_diary_recall_embed(job: ClaimedJob) -> JobResult:
    if job.user_id is None:
        raise JobFatal("missing_user")
    payload = job.payload if isinstance(job.payload, dict) else {}
    try:
        diary_id = uuid.UUID(str(payload.get("diary_id")))
    except (TypeError, ValueError) as e:
        raise JobFatal("invalid_diary_id") from e
    uid = job.user_id

    async with get_sessionmaker()() as session:
        loaded = await diary_recall_repo.embed_diary_document(
            session, user_id=uid, diary_id=diary_id
        )
    if loaded is None:
        # 비공개로 바뀌었거나 삭제됐거나 계정 삭제 차단에 걸린 일기다. 다시 시도해도 같으므로
        # 실패로 두지 않고 정상 종료한다. 본문이 다시 바뀌면 upsert가 새 잡을 등록한다.
        return JobResult(result_code="not_eligible")

    search_text, generation, source_hash, embedding_model, index_version = loaded
    try:
        vector = await diary_recall_repo.embed_text(search_text)
    except Exception as e:  # noqa: BLE001  임베딩 제공자 장애 — 간격을 두고 다시 시도한다
        raise JobRetry("embed_failed") from e

    async def _apply(session) -> None:
        # 쓰기는 (본문 해시, 세대, 모델, 색인 버전)이 그대로일 때만 반영된다. 임베딩을 만드는
        # 사이에 일기가 수정됐으면 0행이 되는데, 그 수정이 이미 새 잡을 등록했으므로 여기서
        # 되살릴 필요가 없다.
        written = await diary_recall_repo.write_diary_embedding(
            session,
            user_id=uid,
            diary_id=diary_id,
            source_hash=source_hash,
            generation=generation,
            embedding_model=embedding_model,
            index_version=index_version,
            vector=vector,
        )
        if not written:
            _log.info(
                "diary_recall_embed 쓰기 대상 없음(그 사이 일기가 바뀜) diary=%s", diary_id
            )

    return JobResult(result_code="ok", apply_domain=_apply)


consumer.register(JOB_DIARY_RECALL_EMBED, handle_diary_recall_embed)
