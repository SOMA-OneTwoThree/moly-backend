"""캐시 fixture 스크립트의 안전장치.

실 provider 호출이 있는 스크립트라 CI에서 실행하지 않는다. 대신 **비용을 내는 경로가
실수로 열리지 않는지**를 고정한다.
"""
from __future__ import annotations

import ast
import pathlib

_SRC = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "verify_prompt_cache.py"


def test_refuses_to_call_provider_without_explicit_yes():
    """--yes 없이는 호출이 일어나면 안 된다. import 만으로 과금되면 안 된다."""
    tree = ast.parse(_SRC.read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "main")
    guard = fn.body[0]
    assert isinstance(guard, ast.If), "main의 첫 문장이 --yes 가드가 아니다"
    assert "yes" in ast.dump(guard.test)


def test_compares_correct_and_forbidden_ordering():
    """올바른 배치만 재보면 '캐시가 걸린다'만 알 뿐, 배치가 원인인지 모른다.

    설계가 주장하는 건 '휘발값을 뒤에 두는 것'이 캐시를 만든다는 인과다. 금지 배치를
    같이 재야 그 주장이 검증된다.
    """
    src = _SRC.read_text()
    assert "good_convo_a" in src and "bad_convo_a" in src


def test_prefix_is_long_enough_to_be_cacheable():
    """1024토큰 미만이면 OpenAI가 캐시를 아예 안 건다 — 통과해도 의미가 없다."""
    from app.services.llm import _OPENAI_CACHE_MIN_PREFIX_TOKENS

    src = _SRC.read_text()
    assert str(_OPENAI_CACHE_MIN_PREFIX_TOKENS) in src
