"""교차 도메인 테스트의 정규화 기억 기본 대역.

대부분의 chat 단위 테스트는 토큰·응답·체크포인트만 검증한다. 이들에서 비동기 기억 producer와
관계 프로필 SQL까지 함께 흉내내면 관심사가 섞이므로 기본은 빈 프로필/no-op producer로 둔다.
기억 배선 테스트는 각 테스트에서 이 대역을 명시적으로 교체한다.
"""
import pytest

from app.services import chat, relationship_profile_repo


@pytest.fixture(autouse=True)
def _unified_memory_defaults(monkeypatch, request):
    async def _empty_profile(*args, **kwargs):
        return ""

    async def _no_source(*args, **kwargs):
        return None

    if request.module.__name__ != "tests.test_relationship_profile_repo":
        monkeypatch.setattr(relationship_profile_repo, "prompt_text", _empty_profile)
    monkeypatch.setattr(chat, "_record_memory_source", _no_source)
