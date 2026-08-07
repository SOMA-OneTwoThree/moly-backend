"""mem0 ingest 파이프라인 — 조각들을 하나의 흐름으로 잇는다.

기억 재설계(docs/ARCHITECTURE-capi.md 5.2절).

    source turn → extractor → eligibility → planned 후보(결정 UUID)
                → batch embedding(1회) → vector upsert → registry pending

**provider 호출과 DB 쓰기의 순서가 이 모듈의 전부다.**
 1. 후보를 `planned`로 **먼저** 저장한다(결정 UUID 포함). provider 성공 직후 crash가 나도
    재시도가 extractor를 다시 부르지 않고 같은 계획을 읽어 같은 id로 upsert한다.
 2. embedding은 통과 후보 전체를 **한 번에** 부른다. 후보마다 부르면 비용과 지연이 배로 든다.
 3. upsert 성공 뒤에야 registry를 `pending`으로 쓴다. registry에 없는 provider 결과는
    검색에서 쓰지 않으므로, 이 순서가 뒤집히면 판정 안 된 기억이 노출된다.

외부 호출은 전부 주입받는다 — 실 provider 없이 흐름을 검증할 수 있어야 한다.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.services import mem0_ingest as mi
from app.services.mem0_budget import BudgetExceeded, StageBudget

_log = logging.getLogger("moly-worker")

# 후보 텍스트 → 임베딩 벡터들(순서 보존). 한 번에 부른다.
EmbedFn = Callable[[list[str], float], Awaitable[list[list[float]]]]
# self-contained source turn → 후보 목록.
ExtractFn = Callable[[float], Awaitable[list[mi.Candidate]]]
# 결정 UUID가 붙은 레코드들을 vector store에 upsert.
UpsertFn = Callable[[list[tuple[uuid.UUID, list[float], dict]], float], Awaitable[list[str]]]
# planned 후보 저장 / registry pending 기록. DB는 호출측이 소유한다.
StageFn = Callable[[list["PlannedCandidate"]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PlannedCandidate:
    """provider 호출 전에 확정되는 계획. 재시도가 이걸 읽어 같은 id로 수렴한다."""

    provider_memory_id: uuid.UUID
    candidate_hash: str
    text: str
    # 재시도로 DB에서 되살린 계획에는 없다(evidence는 이미 candidate_sources에 저장돼 있다).
    candidate: mi.Candidate | None = None

    @property
    def evidence(self) -> tuple:
        """근거 span. 이게 없으면 '어느 발화에서 나온 기억인지'를 영영 알 수 없다."""
        return self.candidate.evidence if self.candidate is not None else ()


@dataclass(slots=True)
class IngestOutcome:
    planned: list[PlannedCandidate] = field(default_factory=list)
    upserted_ids: list[uuid.UUID] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (본문, 사유)
    skipped_reason: str | None = None

    @property
    def no_memory(self) -> bool:
        """정책상 기억 0건인 정상 turn. cursor는 통과시키되 coverage로 남긴다(13.2절)."""
        return not self.planned and self.skipped_reason is None


def plan(
    candidates: list[mi.Candidate],
    *,
    collection_version: str,
    user_id: uuid.UUID,
    turn_seq: int,
) -> list[PlannedCandidate]:
    """통과 후보에 결정 UUID를 붙인다. 이 시점 이후로는 id가 바뀌지 않는다."""
    out: list[PlannedCandidate] = []
    for c in candidates:
        h = mi.candidate_hash(c.text)
        out.append(
            PlannedCandidate(
                provider_memory_id=mi.provider_memory_id(
                    collection_version=collection_version,
                    user_id=user_id,
                    turn_seq=turn_seq,
                    candidate_hash_hex=h,
                ),
                candidate_hash=h,
                text=mi.normalize(c.text),
                candidate=c,
            )
        )
    return out


async def run_ingest(
    *,
    user_id: uuid.UUID,
    turn_seq: int,
    collection_version: str,
    budget: StageBudget,
    extract: ExtractFn,
    embed: EmbedFn,
    upsert: UpsertFn,
    stage_planned: StageFn,
    register_pending: StageFn,
    nickname: str | None = None,
    contract_texts: tuple[str, ...] = (),
    existing_plan: list[PlannedCandidate] | None = None,
    source_texts: dict[int, str] | None = None,
    turns: int = 1,
) -> IngestOutcome:
    """한 source turn을 ingest한다.

    `existing_plan`이 있으면 **extractor를 다시 부르지 않는다** — provider 성공 후 crash가 난
    재시도다. 같은 계획으로 embedding/upsert만 다시 한다(9.2절).
    """
    out = IngestOutcome()

    if existing_plan:
        planned = existing_plan
    else:
        try:
            raw = await extract(budget.timeout_for("extract"))
        except BudgetExceeded as e:
            out.skipped_reason = f"budget:{e.stage}"
            return out
        passed, rejected = mi.filter_candidates(
            raw, nickname=nickname, contract_texts=contract_texts,
            source_texts=source_texts, limit=mi.candidate_limit(turns),
        )
        out.rejected = [(c.text, reason) for c, reason in rejected]
        if not passed:
            return out  # 정책상 기억 0건 — 정상 종결
        planned = plan(
            passed,
            collection_version=collection_version,
            user_id=user_id,
            turn_seq=turn_seq,
        )
        # provider보다 **먼저** 계획을 저장한다.
        await stage_planned(planned)

    out.planned = planned

    try:
        vectors = await embed([p.text for p in planned], budget.timeout_for("embed"))
    except BudgetExceeded as e:
        out.skipped_reason = f"budget:{e.stage}"
        return out
    if len(vectors) != len(planned):
        raise ValueError(
            f"임베딩 개수 불일치: {len(vectors)} != {len(planned)} — 순서 대응이 깨졌다"
        )

    rows = [
        (
            p.provider_memory_id,
            vec,
            {
                # 본문은 provider payload가 보관한다. registry는 본문을 복제하지 않으므로
                # consolidation이 비교 대상 본문을 여기서 hydrate한다(9.4절).
                "text": p.text,
                "user_id": str(user_id),
                "turn_seq": turn_seq,
                "candidate_hash": p.candidate_hash,
                "schema_version": mi.SCHEMA_VERSION,
                "collection_version": collection_version,
            },
        )
        for p, vec in zip(planned, vectors, strict=True)
    ]
    try:
        await upsert(rows, budget.timeout_for("upsert"))
    except BudgetExceeded as e:
        out.skipped_reason = f"budget:{e.stage}"
        return out

    # upsert 성공 뒤에야 registry pending. 순서가 뒤집히면 판정 안 된 기억이 검색에 걸린다.
    await register_pending(planned)
    out.upserted_ids = [p.provider_memory_id for p in planned]
    return out
