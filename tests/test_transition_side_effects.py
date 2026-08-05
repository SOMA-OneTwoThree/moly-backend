"""기억 상태를 닫는 곳은 provider 벡터 정리도 같이 걸어야 한다.

`apply_transitions`가 기억을 duplicate/superseded로 닫으면 `provider_delete_state='pending'`이
된다. 그 pending을 집어가는 것은 `mem0_provider_delete` 잡뿐이고, **그 잡은 스스로 생기지
않는다** — 닫은 쪽이 같은 트랜잭션에서 걸어 줘야 한다.

실제로 `worker/reconsolidate_jobs.py`가 이걸 빠뜨렸다. 증상은 조용했다: 재판정은 "성공"으로
끝나고, 벡터만 provider에 영영 남았다. dev에서 pending 1건이 그렇게 방치돼 cutover 게이트의
`provider delete backlog` 항목에 걸려서야 드러났다.

이런 잡은 앞으로도 늘어난다(닫는 경로가 하나가 아니다). 그래서 개별 잡 테스트 대신 **소스에서
짝을 검사한다** — `apply_transitions`를 부르는 worker 모듈은 provider delete도 걸어야 한다.
"""
from __future__ import annotations

import pathlib

import pytest

_WORKER = pathlib.Path(__file__).resolve().parents[1] / "worker"

# 이 짝을 면제받는 모듈. 면제한다면 **왜인지** 여기 적는다.
_EXEMPT: dict[str, str] = {}


def _modules_closing_memories() -> list[pathlib.Path]:
    out = []
    for p in sorted(_WORKER.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        if "apply_transitions(" in p.read_text(encoding="utf-8"):
            out.append(p)
    return out


def test_some_worker_closes_memories():
    """검사 대상이 0개면 이 파일은 아무것도 지키지 않는다 — 이름이 바뀌었는지 확인할 것."""
    assert _modules_closing_memories(), "apply_transitions를 부르는 worker 모듈을 못 찾았다"


@pytest.mark.parametrize(
    "path", _modules_closing_memories(), ids=lambda p: p.name
)
def test_closing_memories_also_enqueues_provider_delete(path):
    if path.name in _EXEMPT:
        pytest.skip(_EXEMPT[path.name])
    src = path.read_text(encoding="utf-8")
    assert "JOB_MEM0_PROVIDER_DELETE" in src or "enqueue_provider_delete" in src, (
        f"{path.name}이 기억을 닫으면서 provider 벡터 정리를 걸지 않는다. "
        "닫힌 기억의 벡터가 provider에 영영 남는다 — 증상이 조용해서 찾기 어렵다. "
        "정말 필요 없다면 _EXEMPT에 사유와 함께 적어라."
    )
