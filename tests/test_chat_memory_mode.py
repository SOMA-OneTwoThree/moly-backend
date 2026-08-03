"""W10 배선 — chat.py의 기억 **읽기/쓰기 경로 mode 분기**.

cutover(=`chat_contexts.memory_mode='normalized'`)된 유저에게 지켜야 하는 것(명세 §W10 step 6):

- mem0를 **읽지 않는다**(호출 0). 빈 결과도, 장애도, 프로필 미생성도 legacy 문자열로 폴백하지
  않는다 — 폴백하면 forget으로 지운 내용의 mem0 사본이 프롬프트로 되살아난다.
- 스냅샷(`memory_text`)을 **쓰지 않는다**. DB 트리거가 마지막 방어선이지만 애플리케이션도 안 쓴다.
- legacy 유저(=현재 전 유저)는 **동작이 하나도 바뀌지 않는다**.

legacy/normalized를 같은 시나리오로 짝지어 확인한다 — 분기를 지우면 normalized 쪽이 legacy 쪽과
같아지면서 실패한다(한쪽만 보면 분기 삭제를 못 잡는다).
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services import chat as c
from app.services import gating as gating_module
from app.services import llm as llm_module
from app.services import memory as memory_module
from app.services import memory_repo
from app.services.llm import LLMResult
from tests.test_chat import UID, FakeSession, _gating

_SNAPSHOT = "- 유저는 고양이를 키운다"


def _ctx(mode: str, *, memory_text=_SNAPSHOT, refreshed_at=None) -> SimpleNamespace:
    """스냅샷이 **남아 있는** 컨텍스트. normalized면 트리거가 NULL로 만들지만, 행에 값이 있어도
    주입하지 않는다는 것까지 확인하려고 일부러 채워 둔다(심층 방어)."""
    return SimpleNamespace(
        anchor_message_id=0,
        memory_text=memory_text,
        memory_refreshed_at=refreshed_at,
        memory_mode=mode,
    )


class _Session(FakeSession):
    """실행된 SQL 문장을 기록하는 세션(스냅샷 쓰기가 나갔는지 관측)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.statements: list[str] = []

    async def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        return await super().execute(stmt, params)

    def ran(self, sql) -> bool:
        return str(sql) in self.statements


class _Recorder:
    """mem0 호출 횟수 + LLM에 실제로 넘어간 system 블록."""

    def __init__(self, result="- mem0가 준 기억", error: Exception | None = None):
        self.mem_calls = 0
        self.system: list[str] = []
        self._result = result
        self._error = error

    async def load_for_context(self, user_id):
        self.mem_calls += 1
        if self._error is not None:
            raise self._error
        return self._result

    async def generate(self, system, convo, **kw):
        self.system = list(system) if isinstance(system, list) else [system]
        return LLMResult(text="그랬구나.", input_tokens=10, output_tokens=20)

    @property
    def system_text(self) -> str:
        return "\n".join(self.system)


async def _post(session, monkeypatch, rec: _Recorder, *, key="idem-w10"):
    async def _res(s, user_id):
        return _gating()

    # normalized 유저는 turn 배정·추출 잡(W8 배선)도 타는데, 여기서 볼 대상이 아니라 무력화한다.
    async def _alloc(session_, **kw):
        raise memory_repo.NoInboundUserMessageError("이 테스트의 관심사가 아니다")

    monkeypatch.setattr(gating_module, "resolve", _res)
    monkeypatch.setattr(memory_module, "load_for_context", rec.load_for_context)
    monkeypatch.setattr(llm_module, "generate", rec.generate)
    monkeypatch.setattr(memory_repo, "allocate_source_turn", _alloc)
    req = SimpleNamespace(text="오늘 이사했어.", greeting_id=None)
    return await c.post_message(session, UID, req, key)


# ─────────────────────────────────────────────────────────────
# 1. post_message 읽기 경로 — legacy/normalized 짝 비교.
# ─────────────────────────────────────────────────────────────
async def test_legacy_user_still_reloads_and_injects_mem0(monkeypatch):
    """회귀 0 기준선. 현재 전 유저가 여기 해당한다."""
    rec = _Recorder()
    session = _Session(get_map={"ChatContext": _ctx("legacy")})

    out = await _post(session, monkeypatch, rec)

    assert out.reply.content == "그랬구나."
    assert rec.mem_calls == 1                      # 스냅샷이 낡았으니(refreshed_at=None) 재로드
    assert "- mem0가 준 기억" in rec.system_text    # 프롬프트에 주입
    assert session.ran(memory_repo._SAVE_LEGACY_SNAPSHOT_SQL)  # 재로드분 저장


async def test_normalized_user_never_touches_mem0(monkeypatch):
    rec = _Recorder()
    session = _Session(get_map={"ChatContext": _ctx("normalized")})

    out = await _post(session, monkeypatch, rec)

    assert out.reply.content == "그랬구나."          # 대화는 정상
    assert rec.mem_calls == 0                        # mem0 호출 0
    assert "- mem0가 준 기억" not in rec.system_text  # 주입 0
    assert _SNAPSHOT not in rec.system_text          # 행에 남은 legacy 스냅샷도 주입 0
    assert not session.ran(memory_repo._SAVE_LEGACY_SNAPSHOT_SQL)  # 스냅샷 쓰기 0


async def test_normalized_user_ignores_a_fresh_leftover_snapshot(monkeypatch):
    """스냅샷이 **신선**하면 재로드 없이 그대로 주입되는 게 legacy 경로다. normalized는 그 값이
    행에 남아 있어도(트리거 배포 전에 쓰인 잔재) 주입하지 않는다 — 여기가 폴백의 마지막 구멍이다."""
    fresh = datetime.now(timezone.utc) - timedelta(hours=1)  # need_refresh=False
    rec = _Recorder()
    session = _Session(get_map={"ChatContext": _ctx("normalized", refreshed_at=fresh)})

    await _post(session, monkeypatch, rec)

    assert rec.mem_calls == 0
    assert _SNAPSHOT not in rec.system_text

    # 대조군: 같은 조건의 legacy 유저는 그 스냅샷을 그대로 쓴다(회귀 0).
    rec2 = _Recorder()
    session2 = _Session(get_map={"ChatContext": _ctx("legacy", refreshed_at=fresh)})
    await _post(session2, monkeypatch, rec2, key="idem-w10-legacy")
    assert rec2.mem_calls == 0 and _SNAPSHOT in rec2.system_text


async def test_normalized_user_skips_the_reload_call_entirely(monkeypatch):
    """호출측 게이트 — normalized면 `_reload_memory`를 **부르지도 않는다**(핫패스 낭비 0)."""
    calls: list[str] = []

    async def _spy(uid, prev, age_h, *, mode=memory_module.MODE_LEGACY):
        calls.append(mode)
        return "", None

    monkeypatch.setattr(c, "_reload_memory", _spy)
    rec = _Recorder()

    await _post(session := _Session(get_map={"ChatContext": _ctx("normalized")}), monkeypatch, rec)
    assert calls == []
    assert not session.ran(memory_repo._SAVE_LEGACY_SNAPSHOT_SQL)

    # 대조군: legacy는 종전대로 부른다.
    await _post(_Session(get_map={"ChatContext": _ctx("legacy")}), monkeypatch, rec, key="idem-l")
    assert calls == [memory_module.MODE_LEGACY]


async def test_normalized_user_survives_mem0_outage(monkeypatch):
    """mem0가 죽어 있어도 normalized 유저는 애초에 부르지 않으므로 영향이 없다(빈 기억으로 진행)."""
    rec = _Recorder(error=memory_module.MemoryUnavailable("down"))
    session = _Session(get_map={"ChatContext": _ctx("normalized")})

    out = await _post(session, monkeypatch, rec)

    assert out.reply.content == "그랬구나." and rec.mem_calls == 0


async def test_legacy_outage_still_reuses_recent_snapshot(monkeypatch):
    """legacy 4분기(장애+최근 스냅샷 재사용)는 그대로 살아 있다 — normalized 분기가 먹지 않았다."""
    rec = _Recorder(error=memory_module.MemoryUnavailable("down"))
    recent = datetime.now(timezone.utc) - timedelta(hours=7)
    session = _Session(get_map={"ChatContext": _ctx("legacy", refreshed_at=recent)})

    await _post(session, monkeypatch, rec)

    assert rec.mem_calls == 1
    assert _SNAPSHOT in rec.system_text  # 48h 내 스냅샷 재사용


# ─────────────────────────────────────────────────────────────
# 2. _reload_memory — 함수 자체의 게이트(호출측 게이트와 별개 방어선).
# ─────────────────────────────────────────────────────────────
async def test_reload_memory_normalized_returns_empty_without_calling_mem0(monkeypatch):
    calls = {"n": 0}

    async def _load(uid):
        calls["n"] += 1
        return "- 불리면 안 된다"

    monkeypatch.setattr(memory_module, "load_for_context", _load)
    mem, snapshot = await c._reload_memory(
        c._uid(UID), _SNAPSHOT, 1.0, mode=memory_module.MODE_NORMALIZED
    )
    assert (mem, snapshot) == ("", None) and calls["n"] == 0


async def test_reload_memory_normalized_does_not_reuse_previous_snapshot(monkeypatch):
    """legacy의 `빈 성공 → prev 유지` 분기가 normalized에 적용되면 안 된다(지운 기억 부활 경로)."""

    async def _load(uid):
        return ""  # 빈 성공

    monkeypatch.setattr(memory_module, "load_for_context", _load)
    normalized, _ = await c._reload_memory(
        c._uid(UID), _SNAPSHOT, 1.0, mode=memory_module.MODE_NORMALIZED
    )
    legacy, _ = await c._reload_memory(c._uid(UID), _SNAPSHOT, 1.0)
    assert normalized == ""          # 빈 결과 = 빈 기억
    assert legacy == _SNAPSHOT       # legacy는 기존대로 prev 유지


async def test_reload_memory_defaults_to_legacy(monkeypatch):
    async def _load(uid):
        return "- 새 기억"

    monkeypatch.setattr(memory_module, "load_for_context", _load)
    assert await c._reload_memory(c._uid(UID), None, None) == ("- 새 기억", "- 새 기억")


# ─────────────────────────────────────────────────────────────
# 3. _resolve_memory(트랜잭션 안 단일 진입점) — 같은 규칙.
# ─────────────────────────────────────────────────────────────
async def test_resolve_memory_normalized_is_empty_and_writes_nothing(monkeypatch):
    calls = {"n": 0}

    async def _load(uid):
        calls["n"] += 1
        return "- 불리면 안 된다"

    monkeypatch.setattr(memory_module, "load_for_context", _load)
    session = _Session()
    now = datetime.now(timezone.utc)
    out = await c._resolve_memory(
        session, c._uid(UID), _ctx("normalized"), now, mode=memory_module.MODE_NORMALIZED
    )
    assert out == "" and calls["n"] == 0
    assert not session.ran(memory_repo._SAVE_LEGACY_SNAPSHOT_SQL)

    # 신선한 스냅샷(재로드를 건너뛰는 경로)에서도 새면 안 된다 — legacy는 여기서 prev를 그대로 쓴다.
    fresh = _ctx("normalized", refreshed_at=datetime.now(timezone.utc) - timedelta(hours=1))
    assert await c._resolve_memory(
        session, c._uid(UID), fresh, now, mode=memory_module.MODE_NORMALIZED
    ) == ""
    legacy_ctx = _ctx("legacy", refreshed_at=datetime.now(timezone.utc) - timedelta(hours=1))
    assert await c._resolve_memory(session, c._uid(UID), legacy_ctx, now) == _SNAPSHOT


# ─────────────────────────────────────────────────────────────
# 4. 쓰기 경로 — _save_memory는 저장소의 mode-scoped 문장을 쓴다.
# ─────────────────────────────────────────────────────────────
async def test_save_memory_uses_mode_scoped_statement():
    session = _Session()
    now = datetime.now(timezone.utc)
    await c._save_memory(session, c._uid(UID), "- 새 기억", now)
    assert session.ran(memory_repo._SAVE_LEGACY_SNAPSHOT_SQL)
    assert "WHERE chat_contexts.memory_mode = 'legacy'" in str(
        memory_repo._SAVE_LEGACY_SNAPSHOT_SQL
    )


def test_control_tools_stay_empty_while_forget_is_fail_closed():
    """forget이 플래그 뒤에 있는 동안 의도 유입 경로를 열지 않는다(§5 게이트 #5)."""
    from app.services.agent import runtime

    assert runtime.CONTROL_TOOLS == {}
