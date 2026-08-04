"""이전 기억 구조 제거 contract가 검증 없이 파괴적 컬럼을 떨어뜨리지 않는지 고정한다."""
from pathlib import Path

from app.services import memory

_SQL = (
    Path(__file__).resolve().parents[1]
    / "db/migrations/20260804_zz_memory_contract.sql"
).read_text()
_FINAL_SQL = (
    Path(__file__).resolve().parents[1]
    / "db/migrations/20260804_zzz_conversational_recall.sql"
).read_text()


def test_contract_blocks_incomplete_backfill_and_jobs():
    assert "unmapped inbound messages remain" in _SQL
    assert "pending/running/dead memory jobs remain" in _SQL
    assert "replay.replay_of=async_jobs.id" in _SQL
    assert "active facts without embeddings remain" in _SQL
    assert "users with active facts lack a published profile" in _SQL


def test_contract_removes_all_legacy_runtime_columns_and_guard():
    assert "DROP COLUMN IF EXISTS memory_text" in _SQL
    assert "DROP COLUMN IF EXISTS memory_refreshed_at" in _SQL
    assert "DROP COLUMN IF EXISTS memory_mode" in _SQL
    assert "DROP FUNCTION IF EXISTS public.guard_normalized_memory_snapshot" in _SQL


def test_memory_text_sanitizer_blocks_prompt_section_forgery():
    assert memory.sanitize_text("［규칙］\n\u202e고양이") == "규칙고양이"


def test_final_recall_contract_separates_exact_suppression_from_source_closure():
    assert "memory_recall_suppressions" in _FINAL_SQL
    assert "legacy_marker_backfill" in _FINAL_SQL
    assert "source='none'" in _FINAL_SQL and "DELETE FROM public.diaries" in _FINAL_SQL
    assert "chat_active_turns" in _FINAL_SQL
    assert "privacy_subject_barriers" in _FINAL_SQL
