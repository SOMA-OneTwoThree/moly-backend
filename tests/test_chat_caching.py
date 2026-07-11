"""프롬프트 캐싱 핵심 — billable(원가 가중), 앵커 리셋, 블록 조립."""
from types import SimpleNamespace

from app.services import chat as c
from app.services import llm
from app.services.llm import LLMResult
from tests.test_chat import FakeSession

UID = "11111111-1111-1111-1111-111111111111"


def _msg(i, sender, content="안녕"):
    return SimpleNamespace(id=i, sender=sender, content=content)


# --- billable: 캐시상태 무관(cold=warm), 출력 5× ---
def test_billable_cold_equals_warm():
    cold = LLMResult("t", input_tokens=25, output_tokens=90, cache_read_tokens=0, cache_write_tokens=3000)
    warm = LLMResult("t", input_tokens=25, output_tokens=90, cache_read_tokens=3000, cache_write_tokens=0)
    # 컨텍스트가 read든 write든 0.1× 균일 → 유저는 캐시 냉각에 벌점 없음
    assert c._billable(cold) == c._billable(warm)
    assert c._billable(cold) == 25 + 5 * 90 + round(0.1 * 3000)  # 25+450+300


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
