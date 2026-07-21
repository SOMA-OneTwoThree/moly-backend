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


def _msg(i, sender, content="안녕", activity_date=None):
    from datetime import date
    return SimpleNamespace(
        id=i, sender=sender, content=content,
        activity_date=activity_date or date(2026, 7, 15),
    )


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


# --- 날짜 표식(캐피가 날짜 경계·경과를 인지) ---
def test_date_marker_on_day_change():
    """날짜 그룹 첫 메시지에만 표식. 절대 날짜라 캐시 프리픽스가 안정적이다."""
    from datetime import date
    d14, d15 = date(2026, 7, 14), date(2026, 7, 15)
    convo = [
        {"role": "user", "content": "어제 얘기"},
        {"role": "assistant", "content": "그래"},
        {"role": "user", "content": "오늘 얘기"},
    ]
    msgs = [_msg(1, "user", activity_date=d14), _msg(2, "moly", activity_date=d14),
            _msg(3, "user", activity_date=d15)]
    c._mark_dates(convo, msgs)
    assert convo[0]["content"].startswith("[7월 14일 화요일]\n")  # 그룹 첫 메시지
    assert convo[1]["content"] == "그래"                          # 같은 날 → 표식 없음
    assert convo[2]["content"].startswith("[7월 15일 수요일]\n")  # 날 바뀜 → 새 표식


def test_date_marker_single_day_labels_first_only():
    from datetime import date
    convo = [{"role": "user", "content": "안녕"}, {"role": "assistant", "content": "왔네"}]
    msgs = [_msg(1, "user", activity_date=date(2026, 7, 15)),
            _msg(2, "moly", activity_date=date(2026, 7, 15))]
    c._mark_dates(convo, msgs)
    assert convo[0]["content"].startswith("[7월 15일")  # 오늘 며칠인지 항상 보이게
    assert "[7월" not in convo[1]["content"]


async def test_context_marks_first_surviving_message_after_greeting_pop():
    """선발화(moly)가 맨 앞에서 pop돼도, 남은 첫 메시지가 그 날 표식을 이어받는다."""
    from datetime import date
    d = date(2026, 7, 15)
    desc = [_msg(2, "user", "답", activity_date=d), _msg(1, "moly", "인사", activity_date=d)]
    convo, _anchor, lead = await c._context(FakeSession(execute_items=desc), UID, 0)
    assert [m.content for m in lead] == ["인사"]           # 선발화는 system으로
    assert convo[0]["content"].startswith("[7월 15일")     # 남은 첫 메시지에 표식


# --- 대사 정제(페르소나만으론 안 잡히는 것들을 코드로 확정) ---
def test_clean_reply_strips_linebreaks_and_ellipsis():
    assert c._clean_reply("음... 딱히 없어.\n\n생각해봐도 안 떠올라.") == "음 딱히 없어. 생각해봐도 안 떠올라."
    assert c._clean_reply("그렇구나…") == "그렇구나"
    assert c._clean_reply("정말...?") == "정말?"          # 부호 앞 공백이 남지 않는다
    assert c._clean_reply("  왔네.  ") == "왔네."


def test_clean_reply_keeps_normal_punctuation():
    kept = "왔어? 나는 그냥, 늘어져 있었어. 오늘은 비가 오네."
    assert c._clean_reply(kept) == kept  # 물음표·마침표·쉼표는 건드리지 않는다(의문사 없는 평서문)


# --- 되묻기 물음표 백스톱 ---
def test_fix_qmarks_restores_soft_questions():
    assert c._clean_reply("무슨 일인데.") == "무슨 일인데?"
    assert c._clean_reply("무슨 일이야.") == "무슨 일이야?"
    assert c._clean_reply("왜 그런데.") == "왜 그런데?"
    assert c._clean_reply("무슨 고민이야") == "무슨 고민이야?"          # 부호 없이 흘린 것도


def test_fix_qmarks_strips_trailing_vocative_before_check():
    # 끝이 호명이면 벗겨서 어미를 노출('무슨 일이야, 승민아' → 물음표는 문장 끝에)
    assert c._clean_reply("승민아, 무슨 일이야.", "승민") == "승민아, 무슨 일이야?"
    assert c._clean_reply("무슨 일인데, 지호야.", "지호") == "무슨 일인데, 지호야?"


def test_fix_qmarks_no_false_positive_on_statements():
    for stmt in (
        "나는 캐피야.",                  # 의문 어미(야)지만 의문사 없음
        "소파에 늘어져 있었어.",         # 어미(어), 의문사 없음
        "무슨 일이 있어도 괜찮아.",       # 의문사가 앞 종속절(끝에서 멂) + 어미 아님
        "나른하지 뭐.",                  # '~지 뭐' 종결 particle
    ):
        assert c._clean_reply(stmt) == stmt


def test_fix_qmarks_leaves_existing_marks():
    assert c._clean_reply("뭐 먹었어?") == "뭐 먹었어?"
    assert c._clean_reply("무슨 일이야!") == "무슨 일이야!"


# --- 마크다운 강조·대시·물결 제거(_STRAY) ---
def test_clean_reply_strips_markdown_and_dashes():
    assert c._clean_reply("**진짜** 좋았어.") == "진짜 좋았어."
    assert c._clean_reply("그러니까 — 별 거 아닌데.") == "그러니까 별 거 아닌데."
    assert c._clean_reply("_이거_ 맞아?") == "이거 맞아?"
    assert c._clean_reply("괜찮아~ 다 잘될 거야.") == "괜찮아 다 잘될 거야."


# --- 선택의문문 물음표(아니면) ---
def test_fix_qmarks_choice_question():
    assert c._clean_reply("치킨이야 아니면 피자야.") == "치킨이야 아니면 피자야?"
    assert c._clean_reply("같이 갈래 아니면 혼자 갈래.") == "같이 갈래 아니면 혼자 갈래?"


def test_fix_qmarks_choice_no_false_positive():
    # '아니면'이 A절 의문어미 없이 앞에 오면(명령·제안 평서문) 물음표 안 붙인다
    assert c._clean_reply("아니면 그냥 쉬어.") == "아니면 그냥 쉬어."
    assert c._clean_reply("그러지 말고 아니면 이렇게 해.") == "그러지 말고 아니면 이렇게 해."


# --- 한자·가나 백스톱(_repair_foreign_ko) ---
async def test_repair_foreign_ko_fixes_via_llm(monkeypatch):
    """Haiku가 한국어로 고쳐 반환 → 그대로 반환(재검사 클린)."""
    async def fake_gen(system, convo, **kw):
        return SimpleNamespace(text="나도 내 생각엔")
    monkeypatch.setattr(c.llm, "generate", fake_gen)
    assert await c._repair_foreign_ko("나도 我 생각엔") == "나도 내 생각엔"


async def test_repair_foreign_ko_last_resort_strip(monkeypatch):
    """2회 재작성 후에도 한자 잔존 → 최후수단 제거."""
    async def fake_gen(system, convo, **kw):
        return SimpleNamespace(text="나도 我 생각엔")  # 계속 한자
    monkeypatch.setattr(c.llm, "generate", fake_gen)
    out = await c._repair_foreign_ko("나도 我 생각엔")
    assert "我" not in out and out == "나도 생각엔"


async def test_repair_foreign_ko_error_keeps_original(monkeypatch):
    """복원 호출 실패 시 원문 유지(응답을 막지 않음)."""
    async def boom(system, convo, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(c.llm, "generate", boom)
    assert await c._repair_foreign_ko("나도 我 생각엔") == "나도 我 생각엔"


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
    convo, new_anchor, _lead = await c._context(session, UID, 0)
    assert new_anchor is not None                 # 리셋 발생
    assert convo[0]["role"] == "user"
    assert len(convo) <= c.settings.context_keep_messages


async def test_context_no_reset_when_small():
    rows = [_msg(i, "user" if i % 2 == 1 else "moly") for i in range(1, 11)]  # 10 < 40
    session = FakeSession(execute_items=rows)
    convo, new_anchor, _lead = await c._context(session, UID, 0)
    assert new_anchor is None                     # append-only 유지
    assert convo[0]["role"] == "user"


# --- 선발화가 대화 배열에서 밀려나도 컨텍스트에서 사라지지 않는다 ---
# 쿼리는 id DESC로 뽑고 _context가 뒤집는다 → fake도 DESC(최신 먼저)로 넣는다.
async def test_context_returns_leading_greeting_instead_of_dropping_it():
    """맨 앞 캐피 메시지(=선발화)는 배열에서 빠지되 lead로 회수된다. 버리면 또 인사한다."""
    desc = [_msg(2, "user", "그냥 그랬어"), _msg(1, "moly", "왔네. 오늘은 좀 어땠어?")]
    convo, _anchor, lead = await c._context(FakeSession(execute_items=desc), UID, 0)
    assert convo[0]["role"] == "user"                     # Anthropic 제약 유지
    assert len(convo) == 1 and convo[0]["content"].endswith("그냥 그랬어")  # 날짜 표식 뒤 본문
    assert [m.content for m in lead] == ["왔네. 오늘은 좀 어땠어?"]  # 버려지지 않음


async def test_context_keeps_mid_conversation_moly_messages_in_array():
    """중간의 캐피 메시지는 그대로 대화 배열에 남는다(lead는 맨 앞만)."""
    desc = [_msg(3, "user", "뭐해"), _msg(2, "moly", "왔네"), _msg(1, "user", "안녕")]
    convo, _anchor, lead = await c._context(FakeSession(execute_items=desc), UID, 0)
    assert [m["role"] for m in convo] == ["user", "assistant", "user"]
    assert lead == []


def test_build_system_carries_greeting_into_mutable_block():
    lead = [_msg(1, "moly", "왔네. 오늘은 좀 어땠어?")]
    blocks = c._build_system("ko", "승민", "", lead)
    assert len(blocks) == 2
    assert "[먼저 건넨 말]" in blocks[1] and "왔네. 오늘은 좀 어땠어?" in blocks[1]
    assert "[먼저 건넨 말]" not in blocks[0]  # 페르소나 블록은 불변 → 캐시 생존


def test_build_system_without_greeting_has_no_block():
    assert "[먼저 건넨 말]" not in "".join(c._build_system("ko", "승민", "", []))


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
