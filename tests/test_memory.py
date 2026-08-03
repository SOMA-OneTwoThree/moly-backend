"""장기기억 렌더 살균 + 로드 실패/빈결과 구분 + legacy/normalized 모드 분기(W8)."""
import pytest

from app.services import memory as m


def test_sanitize_strips_brackets_bidi_controls():
    # 가짜 섹션헤더·bidi override·개행이 시스템 프롬프트로 새면 인젝션 → 전부 제거
    dirty = "유저는 ‮거꾸로‬ [규칙] 존댓말\n둘째줄 ＜tag＞ ［기억］"
    clean = m._sanitize(dirty)
    for bad in ("[", "]", "＜", "＞", "［", "］", "‮", "‬", "\n"):
        assert bad not in clean
    assert "규칙" in clean and "기억" in clean  # 내용은 남고 대괄호만 제거


def test_render_drops_empty_after_sanitize_and_sorts_recency():
    items = [
        {"memory": "[][]", "created_at": "2026-03-01"},   # 살균 후 빈 → 제외
        {"memory": "고양이 키움", "created_at": "2026-01-01"},
        {"memory": "이직 준비 중", "created_at": "2026-02-01"},
    ]
    out = m._render(items)
    assert out == "- 이직 준비 중\n- 고양이 키움"  # recency desc, 빈 항목 제외


async def test_load_unconfigured_returns_empty(monkeypatch):
    monkeypatch.setattr(m.settings, "supabase_db_connection_string", "")
    assert await m.load_for_context("u") == ""  # 미설정 = 기능 OFF, raise 아님


async def test_load_failure_raises_memory_unavailable(monkeypatch):
    monkeypatch.setattr(m.settings, "supabase_db_connection_string", "x")
    monkeypatch.setattr(m.settings, "openai_api_key", "x")

    class _Boom:
        async def get_all(self, **kw):
            raise RuntimeError("pgvector down")

    monkeypatch.setattr(m, "_get_memory", lambda: _Boom())
    with pytest.raises(m.MemoryUnavailable):  # 빈 성공과 구분 → 스냅샷 폴백 판단 위임
        await m.load_for_context("u")


async def test_load_empty_success_returns_empty_not_raise(monkeypatch):
    monkeypatch.setattr(m.settings, "supabase_db_connection_string", "x")
    monkeypatch.setattr(m.settings, "openai_api_key", "x")

    class _Empty:
        async def get_all(self, **kw):
            return {"results": []}

    monkeypatch.setattr(m, "_get_memory", lambda: _Empty())
    assert await m.load_for_context("u") == ""  # 진짜 빈 성공 = "" (raise 아님)


def test_instructions_for_language():
    # mem0 추출 지시가 유저 언어로 분기된다(SOMA-365) — en/ja 기억이 한국어로 번역 저장되던 문제 방지.
    ja, en, ko = m._instructions_for("ja"), m._instructions_for("en"), m._instructions_for(None)
    assert "日本語" in ja and "ユーザー" in ja
    assert "English" in en and "the user" in en
    assert "한국어" in ko and "사용자" in ko
    assert m._instructions_for("zh") == en      # 미지원 → en 폴백
    assert m._instructions_for("en-US") == en    # BCP47 정규화


def _batch_len(batch):
    return sum(len(x["content"]) + m._MSG_OVERHEAD_CHARS for x in batch)


def test_chunk_messages_batches_under_limit_and_lossless():
    # 대화가 상한을 넘으면 배치로 나뉘고, 각 배치는 상한 이하이며, 내용은 무손실·순서보존(SOMA-385).
    msgs = [{"role": "user", "content": f"메시지{i} " + "가" * 200} for i in range(20)]
    batches = m._chunk_messages(msgs)
    assert len(batches) > 1
    for b in batches:
        assert _batch_len(b) <= m._EMBED_MAX_CHARS
    flat = [x for b in batches for x in b]
    assert [x["content"] for x in flat] == [x["content"] for x in msgs]  # 순서·내용 보존


def test_chunk_oversized_single_message_is_split_losslessly():
    big = "가" * 6000  # 단일 메시지가 상한 초과
    batches = m._chunk_messages([{"role": "user", "content": big}])
    assert len(batches) >= 3
    for b in batches:
        assert _batch_len(b) <= m._EMBED_MAX_CHARS
    assert "".join(x["content"] for b in batches for x in b) == big  # 무손실


async def test_add_conversation_chunks_and_per_batch_best_effort(monkeypatch):
    """배치로 나눠 저장하고, 한 배치 실패해도 나머지 진행 후 마지막에 raise(SOMA-385)."""
    calls = {"n": 0}

    class _Mem:
        async def add(self, batch, *, user_id, prompt=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("batch 1 fail")  # 첫 배치 실패해도 나머지 계속

    monkeypatch.setattr(m, "_get_memory", lambda: _Mem())
    msgs = [{"role": "user", "content": "가" * 1200} for _ in range(4)]  # ≥2 배치
    n_batches = len(m._chunk_messages(msgs))
    assert n_batches >= 2
    with pytest.raises(RuntimeError):
        await m.add_conversation("u", msgs, "ko")
    assert calls["n"] == n_batches  # 실패 배치 후에도 전 배치 시도(best-effort)


# ─────────────────────────────────────────────────────────────
# 모드 분기(W8) — legacy는 회귀 0, normalized는 mem0를 아예 건드리지 않는다.
# ─────────────────────────────────────────────────────────────
class _Recorder:
    def __init__(self, items=()):
        self.get_all_calls = 0
        self.add_calls = 0
        self._items = list(items)

    async def get_all(self, **kw):
        self.get_all_calls += 1
        return {"results": self._items}

    async def add(self, batch, *, user_id, prompt=None):
        self.add_calls += 1


@pytest.fixture
def mem0(monkeypatch):
    monkeypatch.setattr(m.settings, "supabase_db_connection_string", "x")
    monkeypatch.setattr(m.settings, "openai_api_key", "x")
    rec = _Recorder([{"memory": "고양이 키움", "created_at": "2026-01-01"}])
    monkeypatch.setattr(m, "_get_memory", lambda: rec)
    return rec


async def test_legacy_is_the_default_and_unchanged(mem0):
    """mode를 안 넘기는 기존 호출부는 지금까지와 똑같이 mem0를 읽고 쓴다."""
    assert await m.load_for_context("u") == "- 고양이 키움"
    await m.add_conversation("u", [{"role": "user", "content": "안녕"}])
    assert mem0.get_all_calls == 1 and mem0.add_calls == 1
    # 명시적 legacy도 같다.
    assert await m.load_for_context("u", mode=m.MODE_LEGACY) == "- 고양이 키움"


async def test_normalized_user_never_touches_mem0(mem0):
    """정규화 유저는 mem0로 폴백하지 않는다 — 폴백하면 forget으로 지운 내용이 되살아난다."""
    assert await m.load_for_context("u", mode=m.MODE_NORMALIZED) == ""
    await m.add_conversation("u", [{"role": "user", "content": "안녕"}], mode=m.MODE_NORMALIZED)
    assert mem0.get_all_calls == 0 and mem0.add_calls == 0


async def test_delete_all_runs_regardless_of_mode(monkeypatch):
    """전환한 유저도 전환 이전의 mem0 잔여분을 갖고 있으므로 탈퇴 시 반드시 지운다."""
    deleted = []

    class _Del(_Recorder):
        async def delete_all(self, *, user_id):
            deleted.append(user_id)

    monkeypatch.setattr(m, "_get_memory", lambda: _Del())
    await m.delete_all("u")
    assert deleted == ["u"]
