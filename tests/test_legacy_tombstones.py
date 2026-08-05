"""legacy 망각 결정 이관 — 매핑 계약과 차단 조건.

⚠️ 이건 새 forget 기능이 아니라 **전환 장벽**이다. 과거에 잊어달라고 한 source가 v2에서
되살아나지 않게 한다. 매핑 실패를 조용히 넘기면 그 사고가 그대로 난다.
"""
from __future__ import annotations

import pathlib

_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "migrate_legacy_tombstones.py"
_ns: dict = {}
exec(  # noqa: S102
    compile(_PATH.read_text().split("_env, _rest = split_env_arg")[0], str(_PATH), "exec"), _ns
)


def test_every_legacy_suppression_source_has_a_mapping():
    """세 legacy 표면이 모두 이관 대상에 있어야 한다 — 하나라도 빠지면 그 내용이 되살아난다."""
    assert "memory_recall_suppressions" in _ns["_SUPPRESSIONS"]
    assert "memory_source_closures" in _ns["_CLOSURE_MESSAGES"]
    assert "memory_forget_markers" in _ns["_MARKER_MESSAGES"]
    assert "deleted_at IS NOT NULL" in _ns["_DELETED_DIARIES"]


def test_closure_range_is_half_open_on_from_watermark():
    """closure는 (from, through] 구간이다 — from을 포함하면 안 지운 turn까지 막는다."""
    sql = _ns["_CLOSURE_MESSAGES"]
    assert "source_watermark > c.from_watermark" in sql
    assert "source_watermark <= c.through_watermark" in sql


def test_marker_mapping_only_covers_fact_scope():
    """predicate/all scope는 message로 펼 수 없다 — 따로 걸러 차단 대상으로 만든다."""
    assert "f.fact_id IS NOT NULL" in _ns["_MARKER_MESSAGES"]
    assert "fact_id IS NULL" in _ns["_UNMAPPABLE_MARKERS"]


def test_inserts_are_idempotent_via_partial_unique():
    for sql in (_ns["_INSERT_MESSAGE"], _ns["_INSERT_DIARY"]):
        assert "ON CONFLICT" in sql and "DO NOTHING" in sql
        # 부분 unique index를 추론하려면 WHERE 절이 있어야 한다(실 DB로 검증됨).
        assert "WHERE" in sql


def test_reasons_are_distinct_and_traceable():
    reasons = {
        _ns["REASON_SUPPRESSION"], _ns["REASON_CLOSURE"],
        _ns["REASON_MARKER"], _ns["REASON_DIARY"],
    }
    assert len(reasons) == 4
    assert all(r.startswith("legacy_") for r in reasons)
