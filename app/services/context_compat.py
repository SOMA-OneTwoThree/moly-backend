"""Keep existing records opaque while v5 processes only the visible suffix.

No provider/model calls, summary conversion, source omission grants or commits.
The initial snapshot is captured under the caller's ordinary user transaction.
Every subsequent job carries exactly this immutable generation-bound metadata.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import uuid

from sqlalchemy import text
from sqlalchemy.orm.attributes import set_committed_value

SCHEMA = "legacy-context-baseline-v1"


class CompatibilityError(ValueError):
    pass


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Baseline:
    baseline_id: str
    user_id: str
    generation: int
    privacy_epoch: int
    source_after: int
    legacy_anchor: int
    legacy_checkpoint_id: str | None
    legacy_checkpoint_through: int
    legacy_checkpoint_hash: str | None
    legacy_checkpoint_version: str | None
    legacy_summary_hash: str | None
    legacy_contract_after: int
    legacy_contract_id: str | None
    legacy_contract_hash: str | None
    schema: str = SCHEMA

    def __post_init__(self):
        try:
            for value in (self.baseline_id, self.user_id):
                if str(uuid.UUID(value)) != value:
                    raise ValueError
            for value in (self.legacy_checkpoint_id, self.legacy_contract_id):
                if value is not None and str(uuid.UUID(value)) != value:
                    raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise CompatibilityError("compat_identity") from None
        if self.schema != SCHEMA or any(type(value) is not int or value < 0 for value in (
                self.generation, self.privacy_epoch, self.source_after, self.legacy_anchor,
                self.legacy_checkpoint_through, self.legacy_contract_after)):
            raise CompatibilityError("compat_boundary")
        for value in (self.legacy_checkpoint_hash, self.legacy_summary_hash, self.legacy_contract_hash):
            if value is not None and (not isinstance(value, str) or len(value) != 64
                    or any(character not in "0123456789abcdef" for character in value)):
                raise CompatibilityError("compat_hash")
        if (self.legacy_checkpoint_id is None) != (self.legacy_summary_hash is None):
            raise CompatibilityError("compat_legacy_summary")
        if self.legacy_checkpoint_id is None and (self.legacy_checkpoint_through != 0
                or self.legacy_checkpoint_hash is not None or self.legacy_checkpoint_version is not None):
            raise CompatibilityError("compat_legacy_summary")
        if self.legacy_checkpoint_id is not None and (self.legacy_checkpoint_through <= 0
                or self.legacy_checkpoint_through > self.source_after
                or self.legacy_checkpoint_hash is None
                or not isinstance(self.legacy_checkpoint_version, str)
                or self.legacy_checkpoint_version == "conversation-checkpoint-summary-v5"):
            raise CompatibilityError("compat_legacy_summary")
        if (self.legacy_contract_id is None) != (self.legacy_contract_hash is None):
            raise CompatibilityError("compat_legacy_contract")

    def payload(self) -> dict:
        return asdict(self)

    @property
    def fingerprint(self) -> str:
        return digest(self.payload())

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise CompatibilityError("compat_shape")
        try:
            return cls(**value)
        except TypeError:
            raise CompatibilityError("compat_shape") from None


async def load(session, user_id, generation: int) -> Baseline | None:
    from app.models.chat_context import ChatContext
    ctx = await session.get(ChatContext, uuid.UUID(str(user_id)))
    raw = getattr(ctx, "retention_compat_json", None) if ctx is not None else None
    if raw is None:
        return None
    baseline = Baseline.parse(raw)
    if baseline.user_id != str(user_id):
        raise CompatibilityError("compat_owner")
    if baseline.generation != generation or ctx.memory_generation != generation:
        return None  # A forgotten generation is never promoted into a new one.
    return baseline


async def initialize(session, *, user_id, generation: int, privacy_epoch: int,
                     source_after: int, legacy_anchor: int, legacy_checkpoint,
                     locale: str) -> Baseline:
    """Capture metadata only, with generation and exact old-value CAS. No commit."""
    from app.models.chat_context import ChatContext
    ctx = await session.get(ChatContext, uuid.UUID(str(user_id)))
    if ctx is None or ctx.memory_generation != generation:
        raise CompatibilityError("compat_generation")
    previous = getattr(ctx, "retention_compat_json", None)
    current = await load(session, user_id, generation)
    if current is not None:
        if current.privacy_epoch != privacy_epoch:
            raise CompatibilityError("compat_privacy_epoch")
        return current
    # A previous generation's unversioned legacy contracts are not evidence for
    # a new generation. Preserve them in storage without importing them again.
    stale_generation = previous is not None and Baseline.parse(previous).generation != generation
    contract = None if stale_generation else (await session.execute(text("""SELECT id,document_json
      FROM user_interaction_contracts WHERE user_id=:user_id AND locale=:locale
      AND status IN ('published','superseded') AND validation_json IS NULL
      ORDER BY version DESC,id DESC LIMIT 1"""),
      {"user_id": user_id, "locale": locale})).first()
    baseline = Baseline(str(uuid.uuid4()), str(user_id), generation, privacy_epoch,
        source_after, legacy_anchor,
        str(legacy_checkpoint.id) if legacy_checkpoint is not None else None,
        legacy_checkpoint.through_message_id if legacy_checkpoint is not None else 0,
        legacy_checkpoint.source_hash if legacy_checkpoint is not None else None,
        legacy_checkpoint.version if legacy_checkpoint is not None else None,
        hashlib.sha256(legacy_checkpoint.summary.encode()).hexdigest() if legacy_checkpoint is not None else None,
        int(ctx.contract_processed_through_message_id or 0) if ctx.contract_generation == generation else 0,
        str(contract[0]) if contract is not None else None,
        digest(contract[1]) if contract is not None else None)
    row = (await session.execute(text("""UPDATE chat_contexts
      SET retention_compat_json=CAST(:baseline AS jsonb),retention_compat_summary_invalidated_revision=NULL
      WHERE user_id=:user_id AND memory_generation=:generation
      AND retention_compat_json IS NOT DISTINCT FROM CAST(:previous AS jsonb)
      RETURNING retention_compat_json"""), {"user_id": user_id, "generation": generation,
        "baseline": json.dumps(baseline.payload(), sort_keys=True),
        "previous": json.dumps(previous) if previous is not None else None})).first()
    if row is None:
        raise CompatibilityError("compat_changed")
    set_committed_value(ctx, "retention_compat_json", baseline.payload())
    return baseline


async def matches_current(session, baseline: Baseline) -> bool:
    """Read outside the ORM identity cache before publishing generated content."""
    return (await session.scalar(text("""SELECT EXISTS (SELECT 1 FROM chat_contexts
      WHERE user_id=:user_id AND memory_generation=:generation
      AND retention_compat_json=CAST(:binding AS jsonb))"""),
      {"user_id": uuid.UUID(baseline.user_id), "generation": baseline.generation,
       "binding": json.dumps(baseline.payload(), sort_keys=True)})) is True


async def invalidate_legacy_summary(session, baseline: Baseline, *, revision: int) -> None:
    """Persist only reader invalidation, in the successful correction turn txn.

    The old prose and immutable job/source binding remain untouched. There is
    no sound item-level deletion proof in an opaque legacy summary, so it is
    quarantined as a whole while current facts, contracts and raw suffix survive.
    """
    if type(revision) is not int or revision <= 0:
        raise CompatibilityError('compat_invalidation_revision')
    result = await session.scalar(text("""UPDATE chat_contexts SET
      retention_compat_summary_invalidated_revision=COALESCE(
        retention_compat_summary_invalidated_revision,:revision)
      WHERE user_id=:user_id AND memory_generation=:generation
        AND retention_compat_json=CAST(:binding AS jsonb)
      RETURNING user_id"""), {'user_id': uuid.UUID(baseline.user_id),
        'generation': baseline.generation, 'binding': json.dumps(baseline.payload(), sort_keys=True),
        'revision': revision})
    if result is None:
        raise CompatibilityError('compat_changed')


async def legacy_summary(session, baseline: Baseline) -> str:
    """Read the exact old document; never recalculate it or grant v5 coverage."""
    if baseline.legacy_checkpoint_id is None:
        return ""
    row = (await session.execute(text("""SELECT c.summary,c.source_hash,c.through_message_id,c.version
      FROM conversation_checkpoints c JOIN chat_contexts cc ON cc.user_id=c.user_id
      AND cc.memory_generation=c.memory_generation
      WHERE c.id=:id AND c.user_id=:user_id AND c.memory_generation=:generation
      AND cc.retention_compat_summary_invalidated_revision IS NULL
      AND c.preparation_id IS NULL AND c.publish_state='published'"""),
      {"id": uuid.UUID(baseline.legacy_checkpoint_id), "user_id": uuid.UUID(baseline.user_id),
       "generation": baseline.generation})).first()
    if row is None:
        return ""
    if (row[1] != baseline.legacy_checkpoint_hash or row[2] != baseline.legacy_checkpoint_through
            or row[3] != baseline.legacy_checkpoint_version
            or hashlib.sha256(row[0].encode()).hexdigest() != baseline.legacy_summary_hash):
        raise CompatibilityError("compat_legacy_summary_changed")
    return row[0]
