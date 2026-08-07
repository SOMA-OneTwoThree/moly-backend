"""INSERT가 NOT NULL 컬럼을 빠뜨리지 않는지 소스에서 검사한다.

**이 세션에서 세 번 났다.**

  1. `shadow_prompt_traces` — `:param::jsonb`가 바인드로 오인돼 문법 오류
     (그 계측 코드는 이후 제거됐고, 아래 표에서도 빠졌다. 빈 표만 DB에 남아 있다)
  2. `conversation_checkpoints` — `version` 누락
  3. `relationship_profile_renders` — `render_hash` 누락

셋 다 **예외로 보이지 않는다.** finalize transaction에서 터지면 lease가 만료되고 reaper가
회수해 attempts가 소진될 뿐이라, 로그에는 `lease_expired`만 남고 원인이 드러나지 않는다.

DB 없이 돌아야 하므로 스키마를 여기 적는다. 마이그레이션이 바뀌면 이 표도 갱신해야 하며,
그 부담이 이 검사의 값이다 — NOT NULL을 추가하면서 INSERT를 안 고치면 여기서 걸린다.
"""
from __future__ import annotations

import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]

# 테이블 → INSERT에 반드시 있어야 하는 컬럼(NOT NULL이고 기본값 없음).
# dev 실 스키마에서 뽑았다(2026-08-06).
REQUIRED: dict[str, set[str]] = {
    "relationship_profile_renders": {
        "user_id", "prompt_revision", "profile_relationship_revision",
        "locale", "renderer_version", "rendered_text", "render_hash",
    },
    "user_interaction_contracts": {
        "user_id", "version", "locale", "document_json", "rendered_text", "render_hash",
    },
    "user_schedules": {"user_id", "kind", "timezone_snapshot", "next_due_at"},
    "mem0_memory_sources": {
        "registry_id", "user_id", "source_turn_seq", "source_message_id",
        "source_sender", "evidence_start_utf8", "evidence_end_utf8",
        "source_content_hash", "source_occurred_at", "source_activity_date",
        "authority", "extractor_version",
    },
}


def _sources() -> str:
    out = ""
    for d in ("app", "worker"):
        for p in (_ROOT / d).rglob("*.py"):
            if "__pycache__" not in p.parts:
                out += p.read_text(encoding="utf-8")
    return out


def _insert_column_lists(src: str, table: str) -> list[set[str]]:
    """`INSERT INTO <table> (a, b, c)`의 컬럼 목록들."""
    found = re.findall(
        rf"INSERT\s+INTO\s+(?:public\.)?{re.escape(table)}\s*\(([^)]*)\)",
        src, re.IGNORECASE | re.DOTALL,
    )
    return [{c.strip() for c in re.split(r"[,\s]+", block) if c.strip()} for block in found]


@pytest.mark.parametrize("table", sorted(REQUIRED))
def test_every_insert_supplies_all_required_columns(table):
    """빠뜨리면 finalize가 터지고 `lease_expired`로만 보인다 — 원인을 찾기 매우 어렵다."""
    src = _sources()
    inserts = _insert_column_lists(src, table)
    assert inserts, f"{table}에 INSERT하는 코드를 못 찾았다 — 표가 낡았는지 확인할 것"
    for given in inserts:
        missing = REQUIRED[table] - given
        assert not missing, f"{table} INSERT에 빠진 NOT NULL 컬럼: {sorted(missing)}"


def test_sqlalchemy_text_does_not_use_double_colon_cast():
    """`:param::jsonb`는 캐스트가 바인드로 오인돼 문법 오류가 난다(실측).

    `CAST(:param AS jsonb)`를 쓴다.
    """
    src = _sources()
    bad = re.findall(r":[a-z_]+::[a-z]+", src)
    assert not bad, f"바인드 뒤 :: 캐스트가 있다: {sorted(set(bad))}"
