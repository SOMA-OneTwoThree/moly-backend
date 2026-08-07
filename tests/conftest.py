"""교차 도메인 테스트의 정규화 기억 기본 대역.

대부분의 chat 단위 테스트는 토큰·응답·체크포인트만 검증한다. 이들에서 비동기 기억 producer와
관계 프로필 SQL까지 함께 흉내내면 관심사가 섞이므로 기본은 빈 프로필/no-op producer로 둔다.
기억 배선 테스트는 각 테스트에서 이 대역을 명시적으로 교체한다.
"""
import uuid

import pytest

from app.services import (
    chat_references,
    chat_turns,
    diary,
    privacy,
)


@pytest.fixture(autouse=True)
def _unified_memory_defaults(monkeypatch, request):
    async def _acquire(*args, **kwargs):
        return chat_turns.Lease(token=uuid.uuid4(), turn_seq=1, base_context_revision=0)

    async def _nothing(*args, **kwargs):
        return None

    async def _revision(*args, **kwargs):
        return 1

    async def _refs(*args, **kwargs):
        return []

    async def _focus(*args, **kwargs):
        return ""

    async def _valid_refs(*args, **kwargs):
        return True

    # 기존 chat 단위 테스트는 새 lease/reference/first-turn 저장소와 관심사를 분리한다.
    # 해당 계약은 전용 테스트에서 SQL과 상태 전이를 직접 검증한다.
    if request.module.__name__ != "tests.test_conversational_recall_services":
        monkeypatch.setattr(chat_turns, "acquire", _acquire)
        monkeypatch.setattr(chat_turns, "verify_publish", _nothing)
        monkeypatch.setattr(chat_turns, "finish_publish", _revision)
    if request.module.__name__ != "tests.test_chat_references":
        monkeypatch.setattr(chat_references, "persist_selected", _refs)
        monkeypatch.setattr(chat_references, "load_focus_block", _focus)
        monkeypatch.setattr(chat_references, "validate_selected", _valid_refs)
    if request.module.__name__ != "tests.test_privacy":
        monkeypatch.setattr(privacy, "ensure_subject_active", _nothing)
    if request.module.__name__ != "tests.test_diary":
        monkeypatch.setattr(diary, "ensure_welcome_for_first_committed_turn", _nothing)
