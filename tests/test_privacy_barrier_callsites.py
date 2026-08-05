"""삭제 장벽 판정의 **모든 호출부**가 state를 본다 — 레포 전역 검사.

2단계에서 전 사용자에게 `active` 행을 깔았다. 어느 한 곳이라도 "행 존재 = 차단"으로 남아 있으면
그 경로가 전 사용자에게 막힌다.

⚠️ **파일 목록을 하드코딩하지 않는다.** 처음엔 4개 파일만 검사했는데, 그 목록에 없던 동료의
새 코드 2곳이 같은 함정에 빠졌다(커밋 e886e75). 텍스트 충돌 없이 머지가 성공하고 런타임에만
드러나는 종류라, 검사 범위가 곧 방어 범위다.
"""
from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]
# 레포 전역 — 새 호출부가 어디에 생기든 잡는다.
_SCAN_DIRS = ("app", "worker", "scripts", "db")

# 이 판정을 소유한 모듈. state/epoch를 직접 로드하므로 술어 검사 대상이 아니다.
_OWNER = "app/services/privacy.py"


def _sources() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for d in _SCAN_DIRS:
        for path in (_ROOT / d).rglob("*.py"):
            rel = path.relative_to(_ROOT).as_posix()
            out.append((rel, path.read_text()))
    for path in (_ROOT / "db").rglob("*.sql"):
        out.append((path.relative_to(_ROOT).as_posix(), path.read_text()))
    return out


def _barrier_predicates(text: str) -> list[str]:
    """**읽기 술어**만 — `FROM privacy_subject_barriers` 뒤 260자.

    UPDATE SET의 컬럼 참조(`privacy_subject_barriers.epoch + 1`)는 쓰기라 대상이 아니다.
    """
    return [
        text[m.start(): m.start() + 260]
        for m in re.finditer(r"FROM privacy_subject_barriers", text)
    ]


def test_every_barrier_predicate_filters_by_state():
    """행 존재만 보는 술어가 하나도 없어야 한다 — 레포 어디에 있든."""
    offenders: list[str] = []
    for path, src in _sources():
        if path == _OWNER:
            continue
        for frag in _barrier_predicates(src):
            # state/epoch를 직접 로드하는 쿼리와 마이그레이션 DDL은 판정이 아니다.
            if "SELECT state, epoch" in frag or "information_schema" in frag:
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


def test_scanner_actually_scans_the_repo():
    """검사 범위가 곧 방어 범위다 — 스캐너가 실제로 여러 파일을 훑는지 확인한다."""
    paths = {p for p, _ in _sources()}
    assert len(paths) > 50, "레포 전역 스캔이 아니다"
    assert "app/services/jobs.py" in paths
    assert any(p.startswith("worker/") for p in paths)


def test_scanner_catches_a_planted_violation(tmp_path):
    """스캐너가 실제로 위반을 잡는지 — 통과만 하는 검사는 방어가 아니다."""
    bad = "SELECT 1 FROM privacy_subject_barriers b WHERE b.user_id = x.user_id"
    frags = _barrier_predicates(bad)
    assert frags and "state" not in frags[0]
