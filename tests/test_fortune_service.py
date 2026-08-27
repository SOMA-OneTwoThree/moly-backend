"""오늘 운세 v3 서비스 경계와 공개 projection."""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.errors import AppError
from app.main import app
from app.schemas.fortune import (
    DailyFortuneRevealResponse,
    DailyFortuneStatusResponse,
    FortuneProfilePut,
    FortuneResult,
)
from app.services import fortune


def _result_wire() -> dict:
    category = {"score": 50, "text": ["첫 문장", "둘째 문장"]}
    return {
        "schema_version": 3,
        "locale": "ko",
        "overall": {
            "score": 50,
            "headline": "오늘의 총평",
            "flow": ["첫 흐름", "둘째 흐름", "셋째 흐름"],
            "do": "오늘 해볼 것",
            "pause": "오늘 조심할 것",
        },
        "categories": {
            "love": category,
            "money": category,
            "work": category,
            "energy": category,
        },
        "lucky_color": {"key": "green", "name": "초록", "hex": "#43A047"},
    }


def test_profile_accepts_only_birth_date_and_gender():
    req = FortuneProfilePut(gender="man", birth_date=date(2002, 12, 13))
    assert req.model_dump() == {"gender": "man", "birth_date": date(2002, 12, 13)}
    with pytest.raises(ValidationError):
        FortuneProfilePut.model_validate({
            "gender": "man",
            "birth_date": "2002-12-13",
            "birth_time_known": False,
        })


def test_birth_date_age_and_lower_bound_are_explicit():
    fortune._validate_birth_date(date(2002, 12, 13), today=date(2026, 8, 27))
    with pytest.raises(AppError) as underage:
        fortune._validate_birth_date(date(2012, 8, 28), today=date(2026, 8, 27))
    assert underage.value.code == "UNDER_MINIMUM_AGE"
    with pytest.raises(AppError) as old:
        fortune._validate_birth_date(date(1899, 12, 31), today=date(2026, 8, 27))
    assert old.value.code == "INVALID_BIRTH_DATE"


def test_result_build_is_deterministic_and_has_complete_korean_projection():
    profile = SimpleNamespace(birth_date=date(2002, 12, 13))
    first = fortune._build_result(
        profile=profile,
        today=date(2026, 8, 27),
        timezone_name="Asia/Seoul",
    )
    second = fortune._build_result(
        profile=profile,
        today=date(2026, 8, 27),
        timezone_name="Asia/Seoul",
    )
    assert first == second
    semantic, localized = first
    assert semantic["schema_version"] == 3
    assert 0 <= semantic["overall"]["score"] <= 100
    assert set(semantic["categories"]) == {"love", "money", "work", "energy"}
    assert set(localized) == {"ko"}
    assert len(localized["ko"]["overall"]["flow"]) == 3
    assert all(len(value["text"]) == 2 for value in localized["ko"]["categories"].values())


def test_public_result_matches_frontend_v3_schema():
    semantic, copies = fortune._build_result(
        profile=SimpleNamespace(birth_date=date(2002, 12, 13)),
        today=date(2026, 8, 27),
        timezone_name="Asia/Seoul",
    )
    row = SimpleNamespace(semantic_result=semantic, copy_by_locale=copies)
    value = fortune._public_result(row, "ja")
    parsed = FortuneResult.model_validate(value)
    assert parsed.schema_version == 3
    assert parsed.locale == "ko"
    assert len(parsed.overall.flow) == 3
    assert len(parsed.categories.love.text) == 2


def test_current_row_rejects_v1_snapshot_and_accepts_matching_v3():
    base = dict(
        fortune_date=date(2026, 8, 27),
        timezone_snapshot="Asia/Seoul",
        profile_revision=2,
        result_schema_version=3,
        semantic_result={"schema_version": 3},
        copy_by_locale={"ko": {}},
    )
    assert fortune._current_row(SimpleNamespace(**base), today=date(2026, 8, 27), timezone_name="Asia/Seoul", revision=2)
    base["result_schema_version"] = 2
    base["semantic_result"] = {"schema_version": 2}
    assert not fortune._current_row(SimpleNamespace(**base), today=date(2026, 8, 27), timezone_name="Asia/Seoul", revision=2)


def test_fortune_routes_are_registered_but_require_authentication():
    client = TestClient(app)
    assert client.get("/daily-fortune/status").status_code == 401
    assert client.get("/fortune-profile").status_code == 401
    assert client.post("/daily-fortune/reveal").status_code == 401


@pytest.mark.asyncio
async def test_disabled_profile_reads_and_deletes_never_touch_fortune_tables(monkeypatch):
    class NoDatabaseAccess:
        async def get(self, *_args, **_kwargs):  # pragma: no cover - must never run
            raise AssertionError("fortune table was queried while feature was disabled")

    monkeypatch.setattr(fortune, "_ready", lambda: False)
    for action in (fortune.get_profile, fortune.delete_profile):
        with pytest.raises(AppError) as caught:
            await action(NoDatabaseAccess(), "10000000-0000-4000-8000-000000000099")
        assert caught.value.code == "FEATURE_UNAVAILABLE"


def test_v2_cleanup_migration_is_targeted_and_preserves_applied_v1_file():
    v1 = open("db/migrations/20260827_daily_fortune.sql", encoding="utf-8").read()
    v2 = open("db/migrations/20260827_daily_fortune_v2.sql", encoding="utf-8").read()
    assert "CREATE TABLE IF NOT EXISTS public.fortune_profiles" in v1
    assert "DROP COLUMN IF EXISTS birth_time" in v2
    assert "ADD COLUMN IF NOT EXISTS result_schema_version" in v2
    assert "revision=revision+1" in v2
    assert "DROP TABLE" not in v2 and "TRUNCATE" not in v2


def test_result_fingerprint_uses_actual_seed_locale():
    row = SimpleNamespace(
        fortune_date=date(2026, 8, 27),
        timezone_snapshot="Asia/Seoul",
        profile_revision=2,
        result_schema_version=3,
        ephemeris_version="e",
        rule_version="r",
        copy_version="c",
        semantic_result={"schema_version": 3},
        copy_by_locale={"ko": {"headline": "고정"}},
        revealed_at=datetime(2026, 8, 27, 1, tzinfo=timezone.utc),
    )
    assert fortune.result_fingerprint(row, "ja") == fortune.result_fingerprint(row, "ko")


def test_status_and_reveal_schemas_reject_impossible_state_combinations():
    with pytest.raises(ValidationError):
        DailyFortuneStatusResponse.model_validate({"available": False, "state": "unseen"})
    with pytest.raises(ValidationError):
        DailyFortuneStatusResponse.model_validate(
            {
                "available": True,
                "state": "locked",
                "access": "ad_required",
                "local_date": "2026-08-27",
                "versions": {"ephemeris": "e", "rules": "r", "copy": "c"},
            }
        )
    for impossible in (
        {
            "available": True,
            "state": "profile_required",
            "access": "unlocked_today",
        },
        {
            "available": True,
            "state": "locked",
            "access": "unlocked_today",
            "local_date": "2026-08-27",
        },
        {
            "available": True,
            "state": "revealed",
            "access": "included",
            "local_date": "2026-08-27",
            "result": _result_wire(),
            "versions": {"ephemeris": "e", "rules": "r", "copy": "c"},
        },
    ):
        with pytest.raises(ValidationError):
            DailyFortuneStatusResponse.model_validate(impossible)
    with pytest.raises(ValidationError):
        DailyFortuneRevealResponse.model_validate(
            {"state": "locked", "access": "included", "local_date": "2026-08-27"}
        )
    with pytest.raises(ValidationError):
        DailyFortuneRevealResponse.model_validate(
            {
                "state": "revealed",
                "access": "unlocked_today",
                "local_date": "2026-08-27",
            }
        )
