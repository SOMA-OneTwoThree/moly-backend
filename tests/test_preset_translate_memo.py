"""운영 원고 번역 메모 — 같은 (원고, 언어)를 유저마다 다시 번역하지 않는다."""
import asyncio
import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import diary_generation as dg
from tests.test_diary_generation import CFG, FakeSession, _patch_common


@pytest.fixture(autouse=True)
def _reset_memo():
    dg.reset_translate_memo()
    yield
    dg.reset_translate_memo()


async def test_same_ment_same_language_translates_once(monkeypatch):
    calls = []

    async def fake(content, language, *, user_id=None, ledger=None):
        calls.append((content, language, user_id))
        return f"[{dg.i18n.resolve(language)}] {len(content)}"

    monkeypatch.setattr(dg, "_translate_preset", fake)
    mid = uuid.uuid4()
    first_user = uuid.uuid4()
    outs = [await dg._translate_preset_cached(mid, "원문", "ja", user_id=first_user)]
    outs += [await dg._translate_preset_cached(mid, "원문", "ja", user_id=uuid.uuid4()) for _ in range(4)]
    assert outs == ["[ja] 2"] * 5 and len(calls) == 1
    assert calls[0][2] == first_user  # 원장은 처음 미스를 낸 유저에게 기록된다
    await dg._translate_preset_cached(mid, "원문", "en")
    await dg._translate_preset_cached(mid, "원문", "en-US")  # 같은 언어 버킷(en)
    assert len(calls) == 2
    await dg._translate_preset_cached(uuid.uuid4(), "원문", "ja")  # 다른 원고
    await dg._translate_preset_cached(mid, "고친 원문", "ja")  # 원문이 바뀌면 새 키
    assert len(calls) == 4


async def test_failure_is_not_cached(monkeypatch):
    n = {"i": 0}

    async def flaky(content, language, *, user_id=None, ledger=None):
        n["i"] += 1
        return content if n["i"] == 1 else "translated"  # 1회차 실패(원문 유지)

    monkeypatch.setattr(dg, "_translate_preset", flaky)
    mid = uuid.uuid4()
    assert await dg._translate_preset_cached(mid, "원문", "ja") == "원문"
    assert await dg._translate_preset_cached(mid, "원문", "ja") == "translated"
    assert await dg._translate_preset_cached(mid, "원문", "ja") == "translated"
    assert n["i"] == 2



@pytest.mark.parametrize("leaked", ["今日は 한가한 日。", "A slow day ㅎㅎ"])
async def test_translation_with_leftover_korean_is_not_cached(monkeypatch, leaked):
    outs = iter([leaked, "clean"])
    calls = []

    async def fake(content, language, *, user_id=None, ledger=None):
        calls.append(1)
        return next(outs)

    monkeypatch.setattr(dg, "_translate_preset", fake)
    mid = uuid.uuid4()
    assert await dg._translate_preset_cached(mid, "한가한 하루.", "ja") == leaked  # 그 유저는 종전처럼 받는다
    assert await dg._translate_preset_cached(mid, "한가한 하루.", "ja") == "clean"
    assert await dg._translate_preset_cached(mid, "한가한 하루.", "ja") == "clean"
    assert len(calls) == 2

async def test_concurrent_misses_coalesce(monkeypatch):
    calls = []

    async def slow(content, language, *, user_id=None, ledger=None):
        calls.append(1)
        await asyncio.sleep(0.01)
        return "t"

    monkeypatch.setattr(dg, "_translate_preset", slow)
    mid = uuid.uuid4()
    outs = await asyncio.gather(*(dg._translate_preset_cached(mid, "원문", "ja") for _ in range(8)))
    assert outs == ["t"] * 8 and len(calls) == 1


async def test_generate_for_user_translates_weekly_entry_once_per_language(monkeypatch):
    """_generate_for_user 경로 — 일본어 유저 3명 + 영어 유저 2명, 같은 주간 원고 → 번역 LLM 2회."""
    _patch_common(monkeypatch, messages=[])
    ment = SimpleNamespace(id=uuid.uuid4(), content="한가한 하루.", weather="rainy")
    monkeypatch.setattr(dg, "_pick_weekly_ment", AsyncMock(return_value=ment))
    calls = []

    async def fake(content, language, *, user_id=None, ledger=None):
        calls.append(language)
        return {"ja": "のんびりした一日。", "en": "A quiet day."}[dg.i18n.resolve(language)]

    monkeypatch.setattr(dg, "_translate_preset", fake)
    profiles = [SimpleNamespace(id=uuid.uuid4(), timezone="Asia/Tokyo", language=lang)
                for lang in ("ja", "ja", "ja", "en", "en-US")]
    policy = dg.DiaryPolicy(date(2026, 9, 7))
    contents = []
    for p in profiles:
        session = FakeSession()
        r = await dg.generate_for_user(session, p, date(2026, 9, 29), CFG, policy=policy)
        assert r["created"] and r["source"] == "preset"
        contents.append(session.added[0].content)
    assert len(calls) == 2
    assert contents == ["のんびりした一日。"] * 3 + ["A quiet day."] * 2


async def test_korean_users_never_translate(monkeypatch):
    _patch_common(monkeypatch, messages=[])
    ment = SimpleNamespace(id=uuid.uuid4(), content="한가한 하루.", weather="rainy")
    monkeypatch.setattr(dg, "_pick_weekly_ment", AsyncMock(return_value=ment))
    translate = AsyncMock()
    monkeypatch.setattr(dg, "_translate_preset", translate)
    p = SimpleNamespace(id=uuid.uuid4(), timezone="Asia/Seoul", language="ko")
    session = FakeSession()
    r = await dg.generate_for_user(session, p, date(2026, 9, 29), CFG, policy=dg.DiaryPolicy(date(2026, 9, 7)))
    assert r["created"] and session.added[0].content == "한가한 하루."
    translate.assert_not_awaited()
