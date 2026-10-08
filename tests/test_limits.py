"""토큰 한도 해석 — app_config 값 우선, 없으면 settings 임의 기본값(엔드포인트 제거와 무관하게 동작)."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import gating
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


async def test_missing_launch_row_falls_back_and_is_warned_once(caplog, monkeypatch):
    """행이 없으면 지금처럼 코드 기본값으로 떨어지고, 프로세스당 한 번만 경고한다."""
    from app.services import limits

    monkeypatch.setattr(limits, "_warned_missing_launch_row", False)
    with caplog.at_level("WARNING", logger="moly-backend"):
        cfg = await effective_token_config(FakeSession([]))
        await effective_token_config(FakeSession([]), raw={})  # 미리 읽어 온 설정에 없어도 같은 판정
    assert cfg["free_launch_until"] == settings.free_launch_until
    assert caplog.text.count("free_launch_until 행 없음") == 1


@pytest.mark.parametrize("value", ["2099-01-01T00:00:00+09:00", None])
async def test_present_launch_row_is_not_warned(caplog, monkeypatch, value):
    """값이 있거나 명시적 null(OFF)이면 행이 있는 것이다."""
    from app.services import limits

    monkeypatch.setattr(limits, "_warned_missing_launch_row", False)
    rows = [SimpleNamespace(key="free_launch_until", value=value)]
    with caplog.at_level("WARNING", logger="moly-backend"):
        cfg = await effective_token_config(FakeSession(rows))
    assert cfg["free_launch_until"] == value
    assert "free_launch_until 행 없음" not in caplog.text


@pytest.mark.parametrize("language,expected", [("en", 11_000), ("en-US", 11_000), ("ko", 15_000), ("ja", 15_000)])
async def test_review_threshold_is_lower_for_english(monkeypatch, language, expected):
    async def _profile(session, user_id):
        return SimpleNamespace(language=language, timezone="Asia/Seoul")

    async def _sub(session, user_id, now):
        return None

    async def _used(session, user_id, activity_date):
        return 0

    monkeypatch.setattr(gating, "_load_profile", _profile)
    monkeypatch.setattr(gating, "_load_active_subscription", _sub)
    monkeypatch.setattr(gating, "_load_tokens_used", _used)
    monkeypatch.setattr(gating, "derive_entitlement", lambda *a: {})

    g = await gating.resolve(
        FakeSession([]), "u", datetime(2026, 10, 8, tzinfo=timezone.utc),
        config_raw={"review_prompt_min_tokens": 15_000},
    )

    assert g.review_min_tokens == expected
