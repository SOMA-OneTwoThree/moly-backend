"""등록된 잡 타입에는 그 잡을 **만드는 코드**가 있어야 한다.

핸들러를 등록하고 테스트까지 붙여도, enqueue하는 곳이 없으면 그 잡은 영원히 실행되지 않는다.
테스트는 전부 통과하고 CI는 초록이며, 사람이 SQL로 직접 넣어보면 동작까지 한다 — 그래서
"됐다"고 착각하기 쉽다.

이 레포에서 실제로 두 번 났다:
 · `enter_shadow()` — 테스트에서만 호출, dev의 shadow 유저는 SQL 수기 투입이었다
 · `shadow_checkpoint` — 핸들러 등록·소비자 import까지 됐는데 enqueue 함수가 없었다

그래서 사람 주의가 아니라 테스트로 고정한다.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from worker import consumer

# ⚠️ parametrize는 **수집 시점**에 평가된다. 여기서 등록하지 않으면 registry가 비어 있어
# 검사가 통째로 skip되고, 그러면 이 파일은 아무것도 지키지 않는다.
consumer._register_handlers()

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCAN_DIRS = ("app", "worker", "scripts")

# 그 잡을 '만드는' 근거로 치지 않는 파일: 핸들러 자신과 등록 지점.
_NOT_EVIDENCE = {"worker/consumer.py"}


def _sources() -> list[tuple[str, str]]:
    out = []
    for d in _SCAN_DIRS:
        for p in (_ROOT / d).rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            out.append((str(p.relative_to(_ROOT)), p.read_text(encoding="utf-8")))
    return out


def _handler_module(job_type: str) -> str | None:
    """그 job_type을 등록한 모듈 경로. 자기 자신은 근거가 아니다."""
    handler = consumer._REGISTRY.get(job_type)
    if handler is None:
        return None
    mod = getattr(handler, "__module__", "")
    return mod.replace(".", "/") + ".py" if mod else None


def _enqueue_sites(job_type: str, sources: list[tuple[str, str]]) -> list[str]:
    """job_type 문자열이 등장하는 파일 중 핸들러·등록 지점이 아닌 곳."""
    own = _handler_module(job_type)
    skip = _NOT_EVIDENCE | ({own} if own else set())
    pattern = re.compile(rf'["\']{re.escape(job_type)}["\']')
    return [
        path for path, text in sources
        if path not in skip and pattern.search(text)
    ]


@pytest.fixture(scope="module")
def sources():
    return _sources()


def test_registry_is_not_empty(sources):
    """registry가 비면 아래 검사가 전부 공회전한다."""
    assert consumer.registered_types()


@pytest.mark.parametrize("job_type", sorted(consumer._REGISTRY))
def test_job_type_has_something_that_creates_it(job_type, sources):
    """핸들러만 있고 만드는 곳이 없으면 그 잡은 영원히 실행되지 않는다."""
    sites = _enqueue_sites(job_type, sources)
    assert sites, (
        f"'{job_type}' 핸들러는 등록됐는데 이 잡을 만드는 코드가 없다. "
        "enqueue 함수를 추가하고 실제 경로에서 부를 것 "
        "(SQL로 직접 넣어 확인하는 건 근거가 아니다)."
    )
