"""Request-local displayed opening. No reads, writes, timers or model calls."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import uuid

from app.services import crisis_guard, i18n
from app.services.context_provenance import HistoryEntry


class OpeningContextError(ValueError):
    """Fixed internal codes only; never include the opening body in errors."""


@dataclass(frozen=True, slots=True, repr=False)
class OpeningSnapshot:
    owner_id: uuid.UUID
    greeting_id: uuid.UUID
    text: str
    issued_at: datetime | None
    activity_date: date
    committed_id: int | None = None

    def __post_init__(self):
        if (not isinstance(self.owner_id, uuid.UUID) or not isinstance(self.greeting_id, uuid.UUID)
                or not isinstance(self.text, str) or type(self.activity_date) is not date
                or self.issued_at is not None and not isinstance(self.issued_at, datetime)
                or self.committed_id is not None and (type(self.committed_id) is not int or self.committed_id <= 0)):
            raise OpeningContextError('opening_snapshot_invalid')
        try:
            self.text.encode('utf-8')
        except UnicodeError:
            raise OpeningContextError('opening_text_invalid') from None

    @property
    def fingerprint(self) -> str:
        identity = [str(self.owner_id), str(self.greeting_id), hashlib.sha256(self.text.encode()).hexdigest(),
                    self.issued_at.isoformat() if self.issued_at is not None else None,
                    self.activity_date.isoformat(), self.committed_id]
        return hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()


def capture(row, *, user_id: uuid.UUID, decoded_text: str) -> OpeningSnapshot | None:
    """The caller checks ownership/commit eligibility BEFORE decrypting the row.

    Nullable legacy issue times remain nullable. There is deliberately no new
    expiration or current-day rule; the existing greeting resolver owns that.
    """
    if row is None or row.user_id != user_id or row.committed_message_id is not None:
        return None
    return OpeningSnapshot(user_id, row.id, decoded_text, row.created_at, row.activity_date, None)


def _require(opening: OpeningSnapshot, user_id: uuid.UUID) -> None:
    if not isinstance(opening, OpeningSnapshot):
        raise OpeningContextError('opening_snapshot_invalid')
    if opening.owner_id != user_id:
        raise OpeningContextError('opening_owner_mismatch')
    if opening.committed_id is not None:
        raise OpeningContextError('opening_already_committed')


class _OpeningEntry(HistoryEntry):
    __slots__ = ('opening_fingerprint',)

    def __init__(self, opening: OpeningSnapshot):
        super().__init__('assistant', opening.text)
        self.opening_fingerprint = opening.fingerprint

    def copy(self):
        # Preserve the helper marker for an idempotent local reassembly. The
        # provider serializer still sees only role/content mapping keys.
        copied = dict.__new__(type(self))
        HistoryEntry.__init__(copied, self['role'], self['content'])
        copied.opening_fingerprint = self.opening_fingerprint
        return copied


def attach(conversation: list[Mapping[str, str]], opening: OpeningSnapshot | None, *, user_id: uuid.UUID):
    """Call after volatile blocks, before budgeting/provider serialization.

    Historical entries and the current user's object retain their identities.
    The uncommitted opening has no persisted SourceRef and cannot be removed
    merely because checkpoint coverage happens to contain matching text.
    """
    if opening is None:
        return conversation
    _require(opening, user_id)
    if (not conversation or conversation[-1].get('role') != 'user'
            or not isinstance(conversation[-1].get('content'), str)
            or getattr(conversation[-1], 'source_ref', None) is not None):
        raise OpeningContextError('opening_current_user_required')
    existing = [entry for entry in conversation if isinstance(entry, _OpeningEntry)]
    if (any(entry.opening_fingerprint != opening.fingerprint or entry.get('content') != opening.text
            or entry.get('role') != 'assistant' or entry.source_ref is not None for entry in existing)
            or len(existing) > 1):
        raise OpeningContextError('opening_context_conflict')
    entry = existing[0] if existing else _OpeningEntry(opening)
    if existing and conversation[-2] is entry:
        return conversation
    history = [item for item in conversation[:-1] if item is not entry]
    return [*history, entry, conversation[-1]]


_REFERENCE_NOTE = {
    'ko': '짧은 답은 바로 앞에 표시된 인사말에 대한 답으로 먼저 이해하고, 그 인사말의 상황 안에서 답해. 상대가 먼저 꺼내지 않은 이전 주제나 제안·계획을 다시 이어가지 마. 다만 상대가 이전 주제를 명시하거나 새 주제를 꺼내면 그 뜻을 따라가.',
    'en': "Interpret a short reply as responding to the opening shown immediately before it, and keep your answer within that opening's situation. Do not resume an older subject, suggestion or plan unless the user brings it up. An explicit reference to an earlier subject or an explicit new subject takes precedence.",
    'ja': '短い返事は、直前に表示された声かけへの返事としてまず理解して、その声かけの話の中で答えて。相手が先に持ち出していない以前の話題や提案・予定を再開しないで。ただし、相手が以前の話題を明示したり、新しい話題を出したりした場合は、その意味を優先して。',
}


def reference_note(language: str | None) -> str:
    """Volatile instruction only; never repeat the opening's body here."""
    return _REFERENCE_NOTE[i18n.resolve(language)]


_IDENTITY_NOTE = {
    'ko': '상대가 현재 대화에 실제 남아 있는 캐피 인사말을 이어 물으면, 그 인사말의 화자와 이야기의 일관성을 유지해. 캐릭터로서 네가 한 말이며 상대가 한 말이 아니야. 네가 했다고 말한 일을 상대의 행동으로 뒤바꾸거나 반대로 부정하지 마. 원문에 없는 장소나 추가 경험은 만들지 말고, 모르는 세부는 모른다고 짧게 답해. 인사말 원문이 현재 대화에 없으면 그 내용이나 경험을 추측하지 마. 상대가 이전 주제나 새 주제를 명시하면 그 뜻을 따라가.',
    'en': "If the user follows up on a Cappy opening that is actually present in the current conversation, preserve its speaker and story continuity. You said it as the character; it was not the user's statement. Do not reverse the action described in your opening or recast it as the user's action. Do not invent places or additional experiences absent from the original; briefly acknowledge details you do not know. If the original opening is absent from the current conversation, do not infer its contents or experiences. Follow an explicit reference to an earlier or new subject.",
    'ja': '相手が、現在の会話に実際に残っているキャピーの声かけについて続けて尋ねたら、その声かけの話者と話の一貫性を保って。キャラクターとしてきみが言ったことで、相手が言ったことではないよ。きみがしたと話したことを相手の行動にすり替えたり、逆に否定したりしないで。原文にない場所や追加の経験は作らず、分からない細部は分からないと短く答えて。声かけの原文が現在の会話になければ、その内容や経験を推測しないで。相手が以前の話題や新しい話題を明示したら、その意味に従って。',
}


def identity_note(language: str | None) -> str:
    """Conditional speaker continuity; no opening body or inferred facts."""
    return _IDENTITY_NOTE[i18n.resolve(language)]


def guard_history(raw_recent: Sequence[crisis_guard.GuardMessage], opening: OpeningSnapshot | None, *,
                  user_id: uuid.UUID, history_limit: int = 6):
    """Separate model projection; NEVER use it as the DB recent-message proof."""
    if opening is None:
        return raw_recent
    _require(opening, user_id)
    if type(history_limit) is not int or not 1 <= history_limit <= 6:
        raise OpeningContextError('opening_history_limit_invalid')
    return [*(raw_recent[-(history_limit - 1):] if history_limit > 1 else []),
            crisis_guard.GuardMessage('assistant', opening.text, 'greeting', None)]
