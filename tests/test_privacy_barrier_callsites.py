"""삭제 장벽 판정의 **모든 호출부**가 state를 본다.

2단계에서 전 사용자에게 `active` 행을 깔았다. 어느 한 곳이라도 "행 존재 = 차단"으로 남아 있으면
그 경로가 전 사용자에게 막힌다 — 실제로 `jobs._SUCCESS_SQL`이 그래서 모든 잡의 성공 확정을
막고 있었다(dev 실측).
"""
from __future__ import annotations

import pathlib
import re

_FILES = [
    "app/services/jobs.py",
    "app/services/diary_recall_repo.py",
    "app/services/projection_repair.py",
    "app/services/privacy.py",
]


def _barrier_predicates(text: str) -> list[str]:
    """**읽기 술어**만 — `FROM privacy_subject_barriers` 뒤 260자.

    UPDATE SET의 컬럼 참조(`privacy_subject_barriers.epoch + 1`)는 쓰기라 대상이 아니다.
    """
    return [
        text[m.start(): m.start() + 260]
        for m in re.finditer(r"FROM privacy_subject_barriers", text)
    ]


def test_every_barrier_predicate_filters_by_state():
    """행 존재만 보는 술어가 하나도 없어야 한다."""
    offenders: list[str] = []
    for path in _FILES:
        src = pathlib.Path(path).read_text()
        for frag in _barrier_predicates(src):
            # state/epoch를 직접 로드하는 쿼리는 판정이 아니라 조회다.
            if "SELECT state, epoch" in frag:
                continue
            if "state" not in frag:
                offenders.append(f"{path}: {frag[:120]!r}")
    assert not offenders, "행 존재만 보는 장벽 술어:\n" + "\n".join(offenders)


def test_job_success_finalize_allows_active_users():
    """이게 막히면 **모든 잡**이 running에 멈추고 lease 만료 루프를 돈다."""
    from app.services import jobs

    sql = str(jobs._SUCCESS_SQL)
    assert "privacy_subject_barriers" in sql
    assert "b.state <> 'active'" in sql


def test_subject_blocked_allows_active_users():
    from app.services import jobs

    assert "state <> 'active'" in str(jobs._SUBJECT_BLOCKED_SQL)


def test_diary_recall_allows_active_users():
    from app.services import diary_recall_repo as d

    src = pathlib.Path(d.__file__).read_text()
    for frag in _barrier_predicates(src):
        assert "state <> 'active'" in frag
