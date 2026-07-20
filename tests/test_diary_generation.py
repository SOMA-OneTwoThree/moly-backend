"""일기 생성 배치 — 개인/캐피 분기·self-check 폴백·멱등·발행시각(DB·LLM mock)."""
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace


from app.config import settings
from app.services import diary_generation as dg
from app.services import llm as llm_module
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

    async def _pick(session, target_date):
        return ment

    monkeypatch.setattr(dg, "_diary_exists", _exists)
    monkeypatch.setattr(dg, "_day_messages", _msgs)
    monkeypatch.setattr(dg, "_tokens_used", _toks)
    monkeypatch.setattr(dg, "_pick_ment", _pick)


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


# --- _pick_ment 2단 우선순위(날짜 지정본 → 랜덤 폴백) ---
class _PickResult:
    def __init__(self, obj):
        self._obj = obj

    def scalars(self):
        return self

    def first(self):
        return self._obj


class _PickSession:
    """execute 1번째 호출 = 날짜 지정본 조회, 2번째 = 랜덤 폴백 조회."""

    def __init__(self, dated=None, pool=None):
        self._returns = [dated, pool]
        self.calls = 0

    async def execute(self, stmt, params=None):
        obj = self._returns[self.calls] if self.calls < len(self._returns) else None
        self.calls += 1
        return _PickResult(obj)


async def test_pick_ment_prefers_dated():
    dated = SimpleNamespace(id=uuid.uuid4(), content="7월 5일 지정 일기.", weather="rainy")
    pool = SimpleNamespace(id=uuid.uuid4(), content="랜덤 풀.", weather="sunny")
    session = _PickSession(dated=dated, pool=pool)
    got = await dg._pick_ment(session, date(2026, 7, 5))
    assert got is dated
    assert session.calls == 1  # 지정본 있으면 폴백 쿼리 안 함(단락)


async def test_pick_ment_falls_back_to_pool_when_no_dated():
    pool = SimpleNamespace(id=uuid.uuid4(), content="랜덤 풀.", weather="sunny")
    session = _PickSession(dated=None, pool=pool)
    got = await dg._pick_ment(session, date(2026, 7, 5))
    assert got is pool
    assert session.calls == 2  # 지정본 없음 → 폴백 쿼리까지


async def test_pick_ment_none_when_both_empty():
    session = _PickSession(dated=None, pool=None)
    got = await dg._pick_ment(session, date(2026, 7, 5))
    assert got is None
    assert session.calls == 2
