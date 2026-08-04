"""이전 기억 구조 제거 contract가 검증 없이 파괴적 컬럼을 떨어뜨리지 않는지 고정한다."""
from pathlib import Path

from app.services import memory

_SQL = (
    Path(__file__).resolve().parents[1]
    / "db/migrations/20260804_zz_memory_contract.sql"
).read_text()


def test_contract_blocks_incomplete_backfill_and_jobs():
    assert "unmapped inbound messages remain" in _SQL
    assert "pending/running/dead memory jobs remain" in _SQL
    assert "active facts without embeddings remain" in _SQL
    assert "users with active facts lack a published profile" in _SQL


def test_contract_removes_all_legacy_runtime_columns_and_guard():
    assert "DROP COLUMN IF EXISTS memory_text" in _SQL
    assert "DROP COLUMN IF EXISTS memory_refreshed_at" in _SQL
    assert "DROP COLUMN IF EXISTS memory_mode" in _SQL
    assert "DROP FUNCTION IF EXISTS public.guard_normalized_memory_snapshot" in _SQL


def test_memory_text_sanitizer_blocks_prompt_section_forgery():
    assert memory.sanitize_text("［규칙］\n\u202e고양이") == "규칙고양이"
