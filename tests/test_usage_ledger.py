"""AI 원가 원장 — 비용 계산과 상태 수렴.

여기서 지키는 것:
 · 없는 단가를 0으로 접지 않는다(공짜로 집계되는 사고 방지).
 · 응답을 잃은 호출이 0원으로 사라지지 않는다.
 · 이미 수렴한 행을 늦게 돌아온 호출이 덮어쓰지 못한다(fencing).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.services import usage_ledger as ul

# asyncio_mode="auto"(pyproject) — async 테스트에 별도 mark를 붙이지 않는다.

_T0 = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)

LUNA = ul.PriceRow(
    catalog_version=1, provider="openai", model="gpt-5.6-luna",
    input_micro_usd=1_000_000, cached_input_micro_usd=100_000,
    cache_write_micro_usd=1_250_000, output_micro_usd=6_000_000, embedding_micro_usd=None,
)
# GPT-4.1 mini는 cache write 단가가 없다 — GPT-5.6의 1.25배를 추정 적용하지 않는다.
MINI = ul.PriceRow(
    catalog_version=1, provider="openai", model="gpt-4.1-mini-2025-04-14",
    input_micro_usd=400_000, cached_input_micro_usd=100_000,
    cache_write_micro_usd=None, output_micro_usd=1_600_000, embedding_micro_usd=None,
)
EMBED = ul.PriceRow(
    catalog_version=1, provider="openai", model="text-embedding-3-small",
    input_micro_usd=None, cached_input_micro_usd=None, cache_write_micro_usd=None,
    output_micro_usd=None, embedding_micro_usd=20_000,
)


# ─────────────────────────────────────────────────────────────
# 1. 비용 계산
# ─────────────────────────────────────────────────────────────
def test_cost_sums_every_component_with_its_own_rate():
    cost = ul.compute_cost(
        LUNA, input_tokens=1000, cached_input_tokens=500,
        cache_write_tokens=200, output_tokens=300,
    )
    # (1000×1.00 + 500×0.10 + 200×1.25 + 300×6.00) / 1M USD = $0.0031
    assert cost == 3100


def test_embedding_rate_matches_public_price():
    """1M 토큰 = $0.02 = 20,000 micro-USD."""
    assert ul.compute_cost(EMBED, embedding_tokens=1_000_000) == 20_000


def test_missing_rate_with_nonzero_tokens_is_undetermined_not_free():
    """단가 NULL인데 토큰이 있으면 비용을 확정하지 않는다. 0으로 접으면 공짜로 집계된다."""
    assert ul.compute_cost(MINI, input_tokens=100, cache_write_tokens=50) is None


def test_missing_rate_with_zero_tokens_is_fine():
    """쓰지 않은 요금의 단가는 요구하지 않는다."""
    assert ul.compute_cost(MINI, input_tokens=1000, output_tokens=500) == 1_200


def test_nonzero_usage_never_rounds_down_to_zero():
    """올림이라 실제로 쓴 호출이 0원으로 기록되지 않는다."""
    assert ul.compute_cost(LUNA, output_tokens=1) == 6
    assert ul.compute_cost(EMBED, embedding_tokens=1) == 1


def test_zero_usage_is_zero():
    assert ul.compute_cost(LUNA) == 0


def test_rounding_happens_once_at_the_end():
    """항목별로 나누면 절삭이 누적된다. 마지막에 한 번만 나눈다."""
    # 각 항목이 단독으로는 1 micro-USD 미만이지만 합치면 유의미하다.
    cost = ul.compute_cost(EMBED, embedding_tokens=30)  # 30×20,000/1M = 0.6 → 1
    assert cost == 1


# ─────────────────────────────────────────────────────────────
# 2. 원장 상태 수렴 — 인메모리 시뮬레이터
# ─────────────────────────────────────────────────────────────
class _Res:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeLedgerDB:
    """ai_usage_ledger 한 테이블만 모사한다. 지원 밖 문장은 조용히 통과시키지 않는다."""

    def __init__(self):
        self.rows: dict[uuid.UUID, dict] = {}

    async def execute(self, stmt, params=None):
        s = str(stmt)
        p = params or {}
        if s == str(ul._BEGIN_SQL):
            self.rows[p["call_id"]] = {
                "status": "started", "cost_micro_usd": None, "price_catalog_version": None,
                "cost_upper_bound_micro_usd": None, "error_code": None,
                "provider_request_id": None, "model_snapshot": None,
                "purpose": p["purpose"], "lane": p["lane"], "attempt": p["attempt"],
            }
            return _Res([])
        if s == str(ul._COMPLETE_SQL):
            return self._transition(p, "completed", {
                "cost_micro_usd": p["cost_micro_usd"],
                "price_catalog_version": p["price_catalog_version"],
                "cache_write_estimated": p["cache_write_estimated"],
                "provider_request_id": p["provider_request_id"],
                "model_snapshot": p["model_snapshot"],
            })
        if s == str(ul._UNKNOWN_SQL):
            return self._transition(p, "unknown_usage", {
                "cost_upper_bound_micro_usd": p["upper_bound"],
                "error_code": p["error_code"],
            })
        if s == str(ul._FAILED_SQL):
            return self._transition(p, "failed", {"error_code": p["error_code"]})
        raise AssertionError(f"시뮬레이터가 모르는 문장: {s[:80]}")

    def _transition(self, p, new_status: str, fields: dict) -> _Res:
        r = self.rows.get(p["call_id"])
        if r is None or r["status"] != "started":  # WHERE status='started' — fencing
            return _Res([])
        r["status"] = new_status
        r.update(fields)
        return _Res([(p["call_id"],)])


@pytest.fixture
def db():
    return _FakeLedgerDB()


async def _start(db, **kw) -> uuid.UUID:
    return await ul.begin(
        db, lane=ul.LANE_FOREGROUND, purpose="chat",
        provider="openai", model="gpt-5.6-luna", now=_T0, **kw,
    )


async def test_begin_then_complete_records_cost_and_catalog_version(db):
    call_id = await _start(db)
    ok = await ul.complete(
        db, call_id, price=LUNA, input_tokens=1000, output_tokens=300, now=_T0
    )
    assert ok is True
    r = db.rows[call_id]
    assert r["status"] == "completed"
    assert r["cost_micro_usd"] == 2800  # 1000×1.00 + 300×6.00
    assert r["price_catalog_version"] == 1  # 가격이 바뀌어도 과거 비용을 재현할 수 있다


async def test_complete_without_price_falls_back_to_unknown_usage(db):
    """단가 미등록 호출을 0원 completed로 만들지 않는다."""
    call_id = await _start(db)
    ok = await ul.complete(db, call_id, price=None, input_tokens=1000, now=_T0)
    assert ok is False
    r = db.rows[call_id]
    assert r["status"] == "unknown_usage"
    assert r["error_code"] == "price_unavailable"
    assert r["cost_micro_usd"] is None


async def test_complete_with_unpriceable_component_falls_back_to_unknown_usage(db):
    """cache write 단가가 없는 모델에 write 토큰이 잡히면 확정하지 않는다."""
    call_id = await _start(db)
    ok = await ul.complete(
        db, call_id, price=MINI, input_tokens=100, cache_write_tokens=50, now=_T0
    )
    assert ok is False
    assert db.rows[call_id]["status"] == "unknown_usage"


async def test_lost_response_is_preserved_with_upper_bound(db):
    call_id = await _start(db)
    ok = await ul.mark_unknown(
        db, call_id, input_tokens=900, upper_bound_micro_usd=1234,
        error_code="response_lost", now=_T0,
    )
    assert ok is True
    r = db.rows[call_id]
    assert r["status"] == "unknown_usage"
    assert r["cost_upper_bound_micro_usd"] == 1234  # 0원으로 숨기지 않는다


async def test_converged_row_is_not_overwritten_by_late_caller(db):
    """늦게 돌아온 호출이 이미 수렴한 행을 되돌리지 못한다."""
    call_id = await _start(db)
    assert await ul.complete(db, call_id, price=LUNA, output_tokens=10, now=_T0) is True
    assert await ul.mark_failed(db, call_id, error_code="timeout", now=_T0) is False
    assert await ul.complete(db, call_id, price=LUNA, output_tokens=999, now=_T0) is False
    r = db.rows[call_id]
    assert r["status"] == "completed" and r["cost_micro_usd"] == 60


async def test_failed_call_records_error_without_cost(db):
    call_id = await _start(db)
    assert await ul.mark_failed(db, call_id, error_code="connect_error", now=_T0) is True
    r = db.rows[call_id]
    assert r["status"] == "failed" and r["cost_micro_usd"] is None


async def test_cache_write_estimate_is_flagged(db):
    """provider가 usage를 안 주고 추정한 값은 '정확한 실비'가 아님을 행에 남긴다."""
    call_id = await _start(db)
    await ul.complete(
        db, call_id, price=LUNA, input_tokens=100, cache_write_tokens=2000,
        cache_write_estimated=True, now=_T0,
    )
    assert db.rows[call_id]["cache_write_estimated"] is True


# ─────────────────────────────────────────────────────────────
# 3. generate() 계측 배선 — 계측이 대화를 깨뜨리지 않는다
# ─────────────────────────────────────────────────────────────
class _Recorder:
    """open_call/close_call/close_failed 호출을 잡아 두는 스텁."""

    def __init__(self):
        self.opened: list[dict] = []
        self.closed: list[dict] = []
        self.failed: list[dict] = []
        self.call_id = uuid.uuid4()

    def install(self, monkeypatch, module):
        async def _open(ctx, *, provider, model):
            self.opened.append({"ctx": ctx, "provider": provider, "model": model})
            return self.call_id

        async def _close(call_id, **kw):
            self.closed.append({"call_id": call_id, **kw})

        async def _failed(call_id, **kw):
            self.failed.append({"call_id": call_id, **kw})

        monkeypatch.setattr(module.usage_ledger, "open_call", _open)
        monkeypatch.setattr(module.usage_ledger, "close_call", _close)
        monkeypatch.setattr(module.usage_ledger, "close_failed", _failed)


def _fake_openai_resp(prompt=2000, completion=50, cached=1500):
    from types import SimpleNamespace

    return SimpleNamespace(
        id="req_abc123",
        model="gpt-5.6-luna-2026-07-01",
        choices=[SimpleNamespace(message=SimpleNamespace(content="응"), finish_reason="stop")],
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        ),
    )


def _install_client(monkeypatch, module, resp, *, raises: Exception | None = None):
    class _Completions:
        async def create(self, **kw):
            if raises is not None:
                raise raises
            return resp

    client = SimpleNamespaceLike(chat=SimpleNamespaceLike(completions=_Completions()))
    monkeypatch.setattr(module, "_get_openai_client", lambda: client)


class SimpleNamespaceLike:
    def __init__(self, **kw):
        self.__dict__.update(kw)


async def test_generate_without_ledger_records_nothing(monkeypatch):
    """계측은 opt-in이다 — ledger를 안 주면 원장을 건드리지 않는다."""
    from app.services import llm

    rec = _Recorder()
    rec.install(monkeypatch, llm)
    _install_client(monkeypatch, llm, _fake_openai_resp())
    await llm.generate("페르소나", [{"role": "user", "content": "hi"}], model="gpt-5.6-luna")
    assert rec.opened == [] and rec.closed == []


async def test_generate_with_ledger_opens_before_and_closes_with_usage(monkeypatch):
    from app.services import llm

    rec = _Recorder()
    rec.install(monkeypatch, llm)
    _install_client(monkeypatch, llm, _fake_openai_resp(prompt=2000, completion=50, cached=1500))
    ctx = ul.LedgerContext(lane=ul.LANE_FOREGROUND, purpose="chat")
    await llm.generate(
        "페르소나", [{"role": "user", "content": "hi"}], model="gpt-5.6-luna", ledger=ctx
    )
    assert len(rec.opened) == 1 and rec.opened[0]["model"] == "gpt-5.6-luna"
    closed = rec.closed[0]
    assert closed["call_id"] == rec.call_id
    assert closed["cached_input_tokens"] == 1500
    assert closed["output_tokens"] == 50
    # 응답이 알려준 실제 snapshot과 request id가 원장으로 넘어간다(invoice 대사 키).
    assert closed["model_snapshot"] == "gpt-5.6-luna-2026-07-01"
    assert closed["provider_request_id"] == "req_abc123"
    # OpenAI는 write usage를 주지 않아 추정 — 실비로 집계되지 않게 표시된다.
    assert closed["cache_write_estimated"] is True


async def test_provider_exception_is_recorded_as_failed_and_reraised(monkeypatch):
    """토큰을 썼는지 알 수 없는 실패를 0원 completed로 만들지 않는다."""
    from app.services import llm

    rec = _Recorder()
    rec.install(monkeypatch, llm)
    _install_client(monkeypatch, llm, None, raises=RuntimeError("boom"))
    ctx = ul.LedgerContext(lane=ul.LANE_BACKGROUND, purpose="diary_generate")
    with pytest.raises(RuntimeError):
        await llm.generate("p", [{"role": "user", "content": "x"}], model="gpt-5.6-luna", ledger=ctx)
    assert rec.closed == []
    assert rec.failed[0]["error_code"] == "RuntimeError"
