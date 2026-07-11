"""프롬프트 캐싱 핵심 — billable(원가 가중), 앵커 리셋, 블록 조립, 기억 스냅샷."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services import chat as c
from app.services import llm
from app.services.llm import LLMResult
from tests.test_chat import FakeSession

UID = "11111111-1111-1111-1111-111111111111"
UID_UUID = uuid.UUID(UID)
NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


def _msg(i, sender, content="안녕"):
    return SimpleNamespace(id=i, sender=sender, content=content)


# --- billable: 실비용 가중(write 1.25× > read 0.1×), 출력 5× ---
def test_billable_matches_real_cost_weights():
    cold = LLMResult("t", input_tokens=25, output_tokens=90, cache_read_tokens=0, cache_write_tokens=3000)
    warm = LLMResult("t", input_tokens=25, output_tokens=90, cache_read_tokens=3000, cache_write_tokens=0)
    # write는 1.25×(실제 더 비쌈), read는 0.1× → billable × 입력단가 = 실제 청구액
    assert c._billable(cold) == 25 + 5 * 90 + round(1.25 * 3000)   # 25+450+3750 = 4225
    assert c._billable(warm) == 25 + 5 * 90 + round(0.1 * 3000)    # 25+450+300  = 775
    assert c._billable(cold) > c._billable(warm)  # cold(write)가 더 비쌈 = 실비용 반영


def test_billable_output_weighted_5x():
    r = LLMResult("t", input_tokens=0, output_tokens=100, cache_read_tokens=0, cache_write_tokens=0)
    assert c._billable(r) == 500


# --- 앵커 유지 창 ---
def test_keep_window_bounds_and_user_first():
    rows = [_msg(i, "user" if i % 2 == 1 else "moly") for i in range(1, 51)]  # 50개
    kept = c._keep_window(rows)
    assert len(kept) <= c.settings.context_keep_messages
    assert kept[0].sender != "moly"      # 첫 메시지 user 보장
    assert kept[-1].id == 50             # 최신 유지


async def test_context_reset_triggers_on_message_count():
    rows = [_msg(i, "user" if i % 2 == 1 else "moly") for i in range(1, 46)]  # 45 > 40 트리거
    session = FakeSession(execute_items=rows)
    convo, new_anchor = await c._context(session, UID, 0)
    assert new_anchor is not None                 # 리셋 발생
    assert convo[0]["role"] == "user"
    assert len(convo) <= c.settings.context_keep_messages


async def test_context_no_reset_when_small():
    rows = [_msg(i, "user" if i % 2 == 1 else "moly") for i in range(1, 11)]  # 10 < 40
    session = FakeSession(execute_items=rows)
    convo, new_anchor = await c._context(session, UID, 0)
    assert new_anchor is None                     # append-only 유지
    assert convo[0]["role"] == "user"


# --- llm 블록 조립 ---
def test_system_blocks_split_each_cached():
    blocks = llm._system_blocks(["페르소나", "기억"], "5m")
    assert len(blocks) == 2
    assert all(b["cache_control"] == {"type": "ephemeral"} for b in blocks)


def test_system_blocks_drops_empty():
    assert len(llm._system_blocks(["페르소나", ""], "5m")) == 1


def test_cache_last_attaches_control_to_final_message():
    convo = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
             {"role": "user", "content": "c"}]
    out = llm._cache_last(convo, "5m")
    assert isinstance(out[-1]["content"], list)
    assert out[-1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert out[0]["content"] == "a"  # 앞 메시지는 그대로(프리픽스 불변)


def test_cache_last_1h_ttl():
    out = llm._cache_last([{"role": "user", "content": "c"}], "1h")
    assert out[-1]["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


# --- 기억 스냅샷 해결 ---
async def test_resolve_memory_fresh_snapshot_skips_mem0(monkeypatch):
    called = {"n": 0}

    async def _load(uid):
        called["n"] += 1
        return "안 불려야 함"

    monkeypatch.setattr(c.memory, "load_for_context", _load)
    ctx = SimpleNamespace(memory_text="- 고양이", memory_refreshed_at=NOW - timedelta(hours=1))
    out = await c._resolve_memory(FakeSession(), UID_UUID, ctx, NOW)
    assert out == "- 고양이" and called["n"] == 0  # 신선 → 핫패스 mem0 없음


async def test_resolve_memory_stale_reloads_and_saves(monkeypatch):
    async def _load(uid):
        return "- 새 기억"

    monkeypatch.setattr(c.memory, "load_for_context", _load)
    ctx = SimpleNamespace(memory_text="- 옛", memory_refreshed_at=NOW - timedelta(hours=7))
    s = FakeSession()
    out = await c._resolve_memory(s, UID_UUID, ctx, NOW)
    assert out == "- 새 기억"  # 6h 초과 → 재로드


async def test_resolve_memory_outage_reuses_recent_snapshot(monkeypatch):
    async def _load(uid):
        raise c.memory.MemoryUnavailable("down")

    monkeypatch.setattr(c.memory, "load_for_context", _load)
    ctx = SimpleNamespace(memory_text="- 최근", memory_refreshed_at=NOW - timedelta(hours=7))
    out = await c._resolve_memory(FakeSession(), UID_UUID, ctx, NOW)
    assert out == "- 최근"  # 48h 내 → 스냅샷 재사용(장애가 기억 지우지 않음)


async def test_resolve_memory_outage_too_old_returns_empty(monkeypatch):
    async def _load(uid):
        raise c.memory.MemoryUnavailable("down")

    monkeypatch.setattr(c.memory, "load_for_context", _load)
    ctx = SimpleNamespace(memory_text="- 아주 옛", memory_refreshed_at=NOW - timedelta(hours=100))
    out = await c._resolve_memory(FakeSession(), UID_UUID, ctx, NOW)
    assert out == ""  # 48h 초과 → 폐기


async def test_resolve_memory_empty_success_keeps_good_snapshot(monkeypatch):
    async def _load(uid):
        return ""  # 전이 위장(인덱스 리빌드 등 예외 없는 빈 결과)

    monkeypatch.setattr(c.memory, "load_for_context", _load)
    ctx = SimpleNamespace(memory_text="- 좋은 스냅샷", memory_refreshed_at=NOW - timedelta(hours=7))
    out = await c._resolve_memory(FakeSession(), UID_UUID, ctx, NOW)
    assert out == "- 좋은 스냅샷"  # 빈 성공이 좋은 스냅샷을 단발로 덮지 않음(EXP-5)
