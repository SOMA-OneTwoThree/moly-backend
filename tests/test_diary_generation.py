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

    async def _pick(session, target_date):
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


async def test_publishes_personal_even_when_self_check_fails(monkeypatch):
    # self-check 비차단: 게이트 통과 유저는 리젝돼도 개인일기 발행(preset 누수 차단).
    ment = SimpleNamespace(id=uuid.uuid4(), content="캐피는 오늘 뒹굴거렸다.", weather="rainy")
    _patch_common(monkeypatch, messages=[_msg("user", "오늘 진짜 힘들었어")], tokens=5000, ment=ment)

    async def _gen(system, convo, *, max_tokens=None, model=None):
        if model == settings.anthropic_model_utility:
            return LLMResult("NO", 1, 1)  # self-check 리젝 — 이제 비차단(로그만)
        return LLMResult("날씨: sunny\n오늘 그 마음이 오래 남았다", 10, 20)

    monkeypatch.setattr(llm_module, "generate", _gen)
    session = FakeSession()
    await dg.generate_for_user(session, PROFILE, date(2026, 7, 5), CFG)
    d = session.added[0]
    assert d.source == "llm"  # 리젝에도 개인일기 발행(preset 아님)
    assert d.weather == "sunny"
    assert d.preset_ment_id is None
    assert "오래 남았다" in d.content


async def test_diary_body_strips_markdown_and_ellipsis(monkeypatch):
    # 일기 본문의 마크다운(**,-)·말줄임표(...)는 저장 전 제거.
    _patch_common(monkeypatch, messages=[_msg("user", "오늘 힘들었어")], tokens=5000)

    async def _gen(system, convo, *, max_tokens=None, model=None):
        if model == settings.anthropic_model_utility:
            return LLMResult("OK", 1, 1)
        return LLMResult("날씨: sunny\n**오늘** 마음이 - 무거웠다... 그래도 괜찮아", 10, 20)

    monkeypatch.setattr(llm_module, "generate", _gen)
    session = FakeSession()
    await dg.generate_for_user(session, PROFILE, date(2026, 7, 5), CFG)
    d = session.added[0]
    assert d.source == "llm"
    assert "**" not in d.content and "..." not in d.content and "…" not in d.content
    assert "오늘 마음이 무거웠다 그래도 괜찮아" in d.content


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


# --- 개인일기 서지컬 복원(깨진문자·한자 부분수정) ---
def test_needs_repair_detects_broken_and_foreign():
    assert dg._needs_repair("저녁 메�뉴 얘기") is True   # 깨짐
    assert dg._needs_repair("天气가 좋다") is True         # 한자
    assert dg._needs_repair("완전 かわいい다") is True     # 가나
    assert dg._needs_repair("깨끗한 일기였다.") is False
    assert dg._needs_repair("") is False


def test_fallback_clean_deterministic():
    assert dg._fallback_clean("저녁 메�뉴 얘기") == "저녁 메뉴 얘기"  # � 제거·재결합
    assert dg._fallback_clean("天气 좋다") == "좋다"                    # 한자 제거 + 정제


async def test_surgical_repair_minimal_fix(monkeypatch):
    """Haiku가 그 부분만 고쳐 반환(유사도 높음·클린) → 그대로 채택."""
    body = "산책하다 수박주스를 마셨다. 天气가 좋아서 기분이 들떴다."
    fixed = "산책하다 수박주스를 마셨다. 날씨가 좋아서 기분이 들떴다."

    async def fake(system, convo, **kw):
        return LLMResult(fixed, 20, 20)
    monkeypatch.setattr(dg.llm, "generate", fake)
    assert await dg._surgical_repair(body) == fixed


async def test_surgical_repair_rejects_over_edit(monkeypatch):
    """원문과 크게 달라진(과편집) 결과는 최소편집 가드가 거부 → 결정적 폴백."""
    body = "산책하다 수박주스를 마셨다. 天气가 좋아서 기분이 들떴다."
    overwrite = "전혀 다른 문장이야 이건 완전히 새로 쓴 거라 원문이랑 안 겹쳐."  # 클린이지만 통째 재작성

    async def fake(system, convo, **kw):
        return LLMResult(overwrite, 20, 20)
    monkeypatch.setattr(dg.llm, "generate", fake)
    assert await dg._surgical_repair(body) == dg._fallback_clean(body)


async def test_surgical_repair_error_falls_back(monkeypatch):
    """복원 호출 실패 시 결정적 폴백(일기 발행을 막지 않음)."""
    body = "저녁 메�뉴 얘기부터 성격까지 소소했다."

    async def boom(system, convo, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(dg.llm, "generate", boom)
    assert await dg._surgical_repair(body) == dg._fallback_clean(body)
