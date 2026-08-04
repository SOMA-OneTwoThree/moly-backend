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
_BACKFILL_SQL = (
    Path(__file__).resolve().parents[1]
    / "db/migrations/20260804_zzzz_conversational_recall_backfill.sql"
).read_text()
_HARDEN_SQL = (
    Path(__file__).resolve().parents[1]
    / "db/migrations/20260804_zzzzz_conversational_recall_hardening.sql"
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


def test_existing_user_backfill_is_rerunnable_and_id_only_at_job_boundary():
    assert "ON CONFLICT DO NOTHING" in _BACKFILL_SQL
    assert "diary-recall-v1:' || d.embedding_model" in _BACKFILL_SQL
    assert "episode-v1:text-embedding-3-small:" in _BACKFILL_SQL
    assert "jsonb_build_object('schema_version','diary-recall-v1','diary_id'" in _BACKFILL_SQL
    assert "jsonb_build_object('schema_version','episode-v1','message_id'" in _BACKFILL_SQL
    assert "ADD COLUMN IF NOT EXISTS embedding_model" in _BACKFILL_SQL


def test_recall_hardening_converges_provenance_and_excludes_deleting_subjects():
    assert "privacy_subject_barriers" in _HARDEN_SQL
    assert "m.created_at<=d.created_at" in _HARDEN_SQL
    assert "DELETE FROM public.diary_claim_sources" in _HARDEN_SQL
    assert "DELETE FROM public.diary_recall_documents" in _HARDEN_SQL
    assert "DELETE FROM public.memory_episodic_messages" in _HARDEN_SQL
    assert "'sha256'" in _HARDEN_SQL
    assert "embedding_repair_attempts BETWEEN 0 AND 3" in _HARDEN_SQL
