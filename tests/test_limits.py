"""토큰 한도 해석 — app_config 값 우선, 없으면 settings 임의 기본값(엔드포인트 제거와 무관하게 동작)."""
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.limits import effective_token_config


class _Scalars:
    def __init__(self, items):
        self._items = items

    def __iter__(self):
        return iter(self._items)


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return _Scalars(self._items)


class FakeSession:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, stmt):
        return _Result(self.rows)


async def test_defaults_when_no_app_config():
    cfg = await effective_token_config(FakeSession([]))
    assert cfg["daily_token_limit"]["free"] == settings.daily_token_limit_free
    assert cfg["daily_token_limit"]["subscriber"] == settings.daily_token_limit_subscriber
    assert cfg["diary_llm_min_tokens"] == settings.diary_llm_min_tokens
    assert cfg["review_prompt_min_tokens"] == settings.review_prompt_min_tokens


async def test_app_config_overrides_defaults():
    rows = [
        SimpleNamespace(key="daily_token_limit", value={"free": 5, "trial": 6, "subscriber": 7}),
        SimpleNamespace(key="diary_llm_min_tokens", value=99),
    ]
    cfg = await effective_token_config(FakeSession(rows))
    assert cfg["daily_token_limit"] == {"free": 5, "trial": 6, "subscriber": 7}
    assert cfg["diary_llm_min_tokens"] == 99
    # 안 온 키는 여전히 기본값
    assert cfg["token_warning_threshold"] == settings.token_warning_threshold


@pytest.mark.parametrize("value", ["3000", True, -1, None])
async def test_invalid_warning_threshold_uses_default(value):
    rows = [SimpleNamespace(key="token_warning_threshold", value=value)]

    cfg = await effective_token_config(FakeSession(rows))

    assert cfg["token_warning_threshold"] == settings.token_warning_threshold


async def test_nonnegative_warning_threshold_override_is_preserved():
    rows = [SimpleNamespace(key="token_warning_threshold", value=0)]

    cfg = await effective_token_config(FakeSession(rows))

    assert cfg["token_warning_threshold"] == 0
