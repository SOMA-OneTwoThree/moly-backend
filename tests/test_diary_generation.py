"""일기 생성 배치 — 개인/캐피 분기·self-check 폴백·멱등·발행시각(DB·LLM mock)."""
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace


from app.config import settings
from app.services import diary_generation as dg
from app.services import llm as llm_module
from app.services import memory as memory_module
from app.services.diary_prompts import parse
from app.services.llm import LLMResult

CFG = {"diary_min_user_chars": 5}  # 개인일기 게이트 = 당일 유저 메시지 문자수
PROFILE = SimpleNamespace(id=uuid.uuid4(), timezone="Asia/Seoul", language="ko")


class FakeSession:
    def __init__(self):
        self.added = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, stmt, params=None):  # 스냅샷 무효화 UPDATE
        return None

    async def commit(self):
        self.committed = True


def _patch_common(monkeypatch, *, exists=False, messages=None, tokens=5000, ment=None):
    async def _exists(session, uid, td):
        return exists

    async def _msgs(session, uid, td):
        return messages if messages is not None else []

    async def _toks(session, uid, td):
        return tokens

    async def _pick(session):
        return ment

    async def _mem(user_id, msgs):
        return None

    monkeypatch.setattr(dg, "_diary_exists", _exists)
    monkeypatch.setattr(dg, "_day_messages", _msgs)
    monkeypatch.setattr(dg, "_tokens_used", _toks)
    monkeypatch.setattr(dg, "_pick_ment", _pick)
    monkeypatch.setattr(memory_module, "add_conversation", _mem)


def _msg(sender, content):
    return SimpleNamespace(sender=sender, content=content)


# --- 순수 파서/발행시각 ---
def test_parse_weather_header():
    weather, body = parse("날씨: sunny\n오늘 지우는 잘 지냈다.")
    assert weather == "sunny"
    assert body == "오늘 지우는 잘 지냈다."


def test_parse_fallback_when_no_header():
    weather, body = parse("그냥 본문만 있음")
    assert weather == "cloudy"
    assert body == "그냥 본문만 있음"


def test_publish_at_is_next_day_9am_local():
    # 2026-07-05 일기 → 07-06 09:00 KST = 07-06 00:00 UTC
    got = dg.publish_at(date(2026, 7, 5), "Asia/Seoul")
    assert got == datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc)


# --- 생성 분기 ---
async def test_personal_diary_when_tokens_above_threshold(monkeypatch):
    _patch_common(monkeypatch, messages=[_msg("user", "오늘 발표했어")], tokens=5000)

    async def _gen(system, convo, *, max_tokens=None, model=None):
        if model == settings.anthropic_model_utility:
            return LLMResult("OK", 1, 1)  # self-check 통과
        return LLMResult("날씨: sunny\n지우는 오늘 발표를 무사히 마쳤다.", 10, 20)

    monkeypatch.setattr(llm_module, "generate", _gen)
    session = FakeSession()
    await dg.generate_for_user(session, PROFILE, date(2026, 7, 5), CFG)
    d = session.added[0]
    assert d.source == "llm"
    assert d.weather == "sunny"
    assert "발표를 무사히" in d.content
    assert session.committed is True


async def test_fallback_to_preset_when_self_check_fails(monkeypatch):
    ment = SimpleNamespace(id=uuid.uuid4(), content="캐피는 오늘 뒹굴거렸다.", weather="rainy")
    # 문자수 게이트 통과(≥5) → 개인일기 시도 → self-check NO → preset 폴백
    _patch_common(monkeypatch, messages=[_msg("user", "오늘 진짜 힘들었어")], tokens=5000, ment=ment)

    async def _gen(system, convo, *, max_tokens=None, model=None):
        if model == settings.anthropic_model_utility:
            return LLMResult("NO", 1, 1)  # self-check 실패 → preset 폴백
        return LLMResult("날씨: sunny\n지어낸 이야기", 10, 20)

    monkeypatch.setattr(llm_module, "generate", _gen)
    session = FakeSession()
    await dg.generate_for_user(session, PROFILE, date(2026, 7, 5), CFG)
    d = session.added[0]
    assert d.source == "preset"
    assert d.content == "캐피는 오늘 뒹굴거렸다."
    assert d.preset_ment_id == ment.id


async def test_moly_diary_when_below_threshold(monkeypatch):
    ment = SimpleNamespace(id=uuid.uuid4(), content="한가한 하루.", weather="cloudy")
    _patch_common(monkeypatch, messages=[_msg("user", "hi")], tokens=100, ment=ment)

    called = {"llm": False}

    async def _gen(system, convo, *, max_tokens=None, model=None):
        called["llm"] = True
        return LLMResult("x", 1, 1)

    monkeypatch.setattr(llm_module, "generate", _gen)
    session = FakeSession()
    await dg.generate_for_user(session, PROFILE, date(2026, 7, 5), CFG)
    assert session.added[0].source == "preset"
    assert called["llm"] is False  # 임계 미달 → LLM 미호출


async def test_idempotent_skips_when_exists(monkeypatch):
    _patch_common(monkeypatch, exists=True)
    session = FakeSession()
    await dg.generate_for_user(session, PROFILE, date(2026, 7, 5), CFG)
    assert session.added == []
    assert session.committed is False


async def test_empty_pool_uses_safe_default(monkeypatch):
    _patch_common(monkeypatch, messages=[], tokens=0, ment=None)  # 미접속 + 풀 없음
    session = FakeSession()
    await dg.generate_for_user(session, PROFILE, date(2026, 7, 5), CFG)
    d = session.added[0]
    assert d.source == "preset"
    assert d.content  # 비어있지 않음(절대 비지 않음)
    assert d.preset_ment_id is None
