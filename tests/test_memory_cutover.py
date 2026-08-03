"""cutover(W10) — legacy write 차단 트리거 / mode-aware 스냅샷 저장 / 전제 미충족 시 legacy 유지.

실 DB에 붙지 않는다. 트리거는 마이그레이션 SQL의 구조로, 애플리케이션 분기는 문장과 시뮬레이터로
검증한다. downgrade 부재는 **레포 전체 소스**를 훑어 못 박는다(한 파일만 보면 새어나간다).
"""
from __future__ import annotations

import pathlib
import re
import uuid

import pytest

from app.services import memory_cutover as cut
from app.services import memory_repo, prompts

@pytest.fixture
def profile_wired(monkeypatch):
    """관계 프로필 배선 게이트를 연다 — 전환 메커니즘 자체를 보는 테스트용.

    게이트가 막는지는 test_cutover_is_blocked_until_the_profile_block_is_wired가 따로 본다.
    """
    monkeypatch.setattr(prompts, "RELATIONSHIP_PROFILE_BLOCK_ENABLED", True)


_UID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MIGRATION = (_ROOT / "db/migrations/20260804_memory_cutover_guard.sql").read_text(encoding="utf-8")
_SCHEMA = (_ROOT / "db/schema.sql").read_text(encoding="utf-8")


class _Res:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return self._rows[0][0]

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, **canned) -> None:
        self.calls: list[tuple[str, object]] = []
        self.mode = canned.get("mode", "legacy")
        self.generation = canned.get("generation", 2)
        self.watermark = canned.get("watermark", 9)
        self.turns_watermark = canned.get("turns_watermark", 9)
        self.pending = canned.get("pending", 0)
        self.dead = canned.get("dead", 0)
        self.done_watermark = canned.get("done_watermark", 9)
        self.published = canned.get("published", True)
        self.language = canned.get("language", "ko")
        self.cutover_hits = canned.get("cutover_hits", True)
        self.has_context = canned.get("has_context", True)

    @property
    def statements(self) -> list[str]:
        return [c[0] for c in self.calls]

    def executed(self, sql) -> list[object]:
        return [p for s, p in self.calls if s == str(sql)]

    def ran(self, sql) -> bool:
        return str(sql) in self.statements

    async def execute(self, stmt, params=None):
        s = str(stmt)
        self.calls.append((s, params))
        if s in (str(cut._LOCK_CONTEXT_SQL), str(cut._CONTEXT_SQL)):
            if not self.has_context:
                return _Res([])
            return _Res([(self.mode, self.generation, self.watermark)])
        if s == str(cut._SOURCE_WATERMARK_SQL):
            return _Res([(self.turns_watermark,)])
        if s == str(cut._SHADOW_SQL):
            return _Res([(self.pending, self.dead, self.done_watermark)])
        if s == str(cut._PUBLISHED_PROFILE_SQL):
            return _Res([(1,)] if self.published else [])
        if s == str(cut._LANGUAGE_SQL):
            return _Res([(self.language,)])
        if s == str(cut._CUTOVER_SQL):
            return _Res([(self.generation + 1,)] if self.cutover_hits else [])
        if s == str(memory_repo._SAVE_LEGACY_SNAPSHOT_SQL):
            return _Res([(_UID,)] if self.mode == "legacy" else [])
        raise AssertionError(f"시뮬레이터가 모르는 문장: {s[:80]}")


# ─────────────────────────────────────────────────────────────
# 1) legacy write 차단 트리거 — normalized 행의 스냅샷은 항상 NULL.
# ─────────────────────────────────────────────────────────────
def test_trigger_is_before_insert_or_update_on_chat_contexts():
    assert "BEFORE INSERT OR UPDATE ON public.chat_contexts" in _MIGRATION
    assert "chat_contexts_normalized_snapshot_guard" in _MIGRATION
    assert "FOR EACH ROW EXECUTE FUNCTION public.guard_normalized_memory_snapshot()" in _MIGRATION


def test_trigger_nullifies_snapshot_columns_on_both_paths():
    body = _MIGRATION[_MIGRATION.index("RETURNS trigger") : _MIGRATION.index("DROP TRIGGER")]
    insert_branch = body[body.index("TG_OP='INSERT'") : body.index("TG_OP='UPDATE'")]
    update_branch = body[body.index("TG_OP='UPDATE'") :]
    for branch in (insert_branch, update_branch):
        assert "NEW.memory_text := NULL;" in branch
        assert "NEW.memory_refreshed_at := NULL;" in branch


def test_trigger_blocks_downgrade():
    """OLD가 normalized면 NEW.memory_mode를 normalized로 되돌린다."""
    assert re.search(
        r"TG_OP='UPDATE' AND OLD\.memory_mode='normalized'.*NEW\.memory_mode := 'normalized'",
        _MIGRATION,
        re.S,
    )


def test_migration_is_additive_only():
    assert not re.search(r"\b(DROP TABLE|DROP COLUMN|ALTER COLUMN)\b", _MIGRATION, re.I)
    # 재적용 안전(트리거는 DROP IF EXISTS 후 재생성 — 함수는 CREATE OR REPLACE).
    assert "DROP TRIGGER IF EXISTS chat_contexts_normalized_snapshot_guard" in _MIGRATION


def test_schema_sql_carries_the_same_trigger():
    assert "guard_normalized_memory_snapshot" in _SCHEMA
    assert "chat_contexts_normalized_snapshot_guard" in _SCHEMA


# ─────────────────────────────────────────────────────────────
# 2) mode-aware 스냅샷 저장 — normalized 행을 legacy 경로가 덮지 못한다.
# ─────────────────────────────────────────────────────────────
def test_snapshot_upsert_is_scoped_to_legacy_rows():
    sql = str(memory_repo._SAVE_LEGACY_SNAPSHOT_SQL)
    assert "WHERE chat_contexts.memory_mode = 'legacy'" in sql
    # 스냅샷 컬럼만 갱신한다(앵커·세대·워터마크 클로버 금지).
    for column in ("anchor_message_id", "memory_generation", "memory_source_watermark"):
        assert column not in sql


async def test_save_legacy_snapshot_reports_skip_for_normalized_user():
    from datetime import datetime, timezone

    now = datetime(2026, 8, 4, tzinfo=timezone.utc)
    legacy = _FakeSession(mode="legacy")
    assert await memory_repo.save_legacy_snapshot(
        legacy, _UID, memory_text="기억", refreshed_at=now
    ) is True

    normalized = _FakeSession(mode="normalized")
    assert await memory_repo.save_legacy_snapshot(
        normalized, _UID, memory_text="기억", refreshed_at=now
    ) is False


# ─────────────────────────────────────────────────────────────
# 3) 전제 검사 — 하나라도 못 채우면 legacy에 남는다.
# ─────────────────────────────────────────────────────────────
async def test_all_preconditions_met_is_eligible():
    decision = await cut.evaluate(_FakeSession(), user_id=_UID)
    assert decision.eligible and decision.reasons == ()


@pytest.mark.parametrize(
    "canned,reason",
    [
        ({"turns_watermark": 7}, cut.REASON_SOURCE_BACKFILL_INCOMPLETE),
        ({"pending": 1}, cut.REASON_SHADOW_INCOMPLETE),
        ({"dead": 1}, cut.REASON_SHADOW_INCOMPLETE),
        ({"done_watermark": 8}, cut.REASON_SHADOW_INCOMPLETE),
        ({"published": False}, cut.REASON_NO_PUBLISHED_PROFILE),
        ({"has_context": False}, cut.REASON_NO_CONTEXT),
    ],
)
async def test_each_precondition_blocks(canned, reason):
    decision = await cut.evaluate(_FakeSession(**canned), user_id=_UID)
    assert not decision.eligible and reason in decision.reasons


async def test_blocked_user_stays_legacy(profile_wired):
    session = _FakeSession(published=False)
    result = await cut.cutover_user(
        session, user_id=_UID, release_inventory_confirmed=True
    )
    assert result.status == cut.RESULT_BLOCKED
    assert cut.REASON_NO_PUBLISHED_PROFILE in result.reasons
    assert not session.ran(cut._CUTOVER_SQL)  # 전환 문장 자체가 실행되지 않았다


async def test_locale_of_published_profile_is_checked():
    session = _FakeSession(language="ja")
    await cut.evaluate(session, user_id=_UID)
    assert session.executed(cut._PUBLISHED_PROFILE_SQL)[0]["locale"] == "ja"


# ─────────────────────────────────────────────────────────────
# 4) 1단계 게이트(운영 절차) — 확인 없이는 아무것도 하지 않는다.
# ─────────────────────────────────────────────────────────────
async def test_inventory_gate_is_closed_by_default():
    session = _FakeSession()
    with pytest.raises(cut.CutoverBlocked):
        await cut.cutover_user(session, user_id=_UID, release_inventory_confirmed=False)
    assert session.calls == []  # 게이트가 닫히면 읽기조차 하지 않는다


# ─────────────────────────────────────────────────────────────
# 5) 전환 문장 — 한 UPDATE로 확정, 재전환 없음, downgrade 없음.
# ─────────────────────────────────────────────────────────────
async def test_cutover_locks_then_updates_in_one_statement(profile_wired):
    session = _FakeSession()
    result = await cut.cutover_user(session, user_id=_UID, release_inventory_confirmed=True)
    assert result.status == cut.RESULT_CUTOVER and result.memory_generation == 3
    assert session.statements.index(str(cut._LOCK_CONTEXT_SQL)) < session.statements.index(
        str(cut._CUTOVER_SQL)
    )
    sql = str(cut._CUTOVER_SQL)
    for fragment in (
        "memory_mode='normalized'",
        "memory_generation = memory_generation + 1",
        "memory_text = NULL",
        "memory_refreshed_at = NULL",
        "memory_mode='legacy'",  # 출발 상태를 WHERE에 못 박는다(재전환 방지)
    ):
        assert fragment in sql


async def test_already_normalized_user_is_left_alone(profile_wired):
    session = _FakeSession(mode="normalized")
    result = await cut.cutover_user(session, user_id=_UID, release_inventory_confirmed=True)
    assert result.status == cut.RESULT_ALREADY_NORMALIZED
    assert not session.ran(cut._CUTOVER_SQL)  # generation을 다시 올리지 않는다


async def test_update_zero_rows_keeps_user_legacy(profile_wired):
    session = _FakeSession(cutover_hits=False)
    result = await cut.cutover_user(session, user_id=_UID, release_inventory_confirmed=True)
    assert result.status == cut.RESULT_BLOCKED


def _set_clauses(src: str) -> list[str]:
    """UPDATE의 SET 절만 잘라낸다(WHERE 절의 `memory_mode='legacy'`는 downgrade가 아니다)."""
    out = []
    for m in re.finditer(r"\bSET\b", src):
        seg = src[m.end() : m.end() + 400]
        end = re.search(r"\bWHERE\b|\bRETURNING\b|;", seg)
        out.append(seg[: end.start()] if end else seg)
    return out


def test_no_downgrade_path_anywhere_in_the_repo():
    """normalized → legacy로 되돌리는 대입이 레포 어디에도 없다(SET 절·plpgsql 대입 모두)."""
    assign = re.compile(r"memory_mode\s*(=|:=)\s*'legacy'")
    paths = (
        list((_ROOT / "app").rglob("*.py"))
        + list((_ROOT / "worker").rglob("*.py"))
        + list((_ROOT / "db").rglob("*.sql"))
    )
    for path in paths:
        src = path.read_text(encoding="utf-8")
        for clause in _set_clauses(src):
            assert not assign.search(clause), path
        assert ":=" not in src or not re.search(r"memory_mode\s*:=\s*'legacy'", src), path


# --- 배선 게이트 (통합 검증 지적) ---
async def test_cutover_is_blocked_until_the_profile_block_is_wired():
    """관계 프로필이 대화에 안 실리는 동안은 전환을 열지 않는다.

    normalized 유저는 mem0로 되돌아가지 않는다(그게 전환의 요지다). 프로필이 프롬프트에
    안 들어가면 기억이 영구히 빈 채로 남고, downgrade도 막혀 있어 되돌릴 수 없다 —
    유저 입장에선 캐피가 자기를 통째로 잊은 것이다.
    """
    assert prompts.RELATIONSHIP_PROFILE_BLOCK_ENABLED is False  # 아직 배선 전

    session = _FakeSession()
    with pytest.raises(cut.CutoverBlocked, match="배선"):
        await cut.cutover_user(session, user_id=_UID, release_inventory_confirmed=True)

    assert session.statements == []  # 게이트가 DB에 닿기 전에 막는다


def test_profile_gate_passes_once_wired(monkeypatch):
    """플래그가 켜지면 게이트는 통과한다(게이트가 영구 차단이 아니다)."""
    monkeypatch.setattr(prompts, "RELATIONSHIP_PROFILE_BLOCK_ENABLED", True)
    cut.assert_profile_block_wired()  # 예외 없음
