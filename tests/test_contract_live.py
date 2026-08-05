"""interaction contract가 실제 대화에 들어간다 (6.3절).

감사 지적: 스키마·compiler·backfill은 있는데 **live에 연결돼 있지 않아** contract 행이
0건이었다. 사용자가 "앞으로 반말로 해줘"라고 해도 다음 대화에서 안 지켜졌다.
"""
from __future__ import annotations

import inspect

import pytest

from app.services import chat, contract_repo
from app.services import interaction_contract as ic
from worker import consumer, contract_jobs


def test_contract_is_read_in_the_chat_path():
    """읽는 곳이 없으면 발행해도 아무 일이 없다."""
    src = inspect.getsource(chat.post_message)
    assert "contract_repo.published_text(" in src


def test_contract_goes_into_the_cached_prefix():
    """계약은 자주 안 바뀐다 — 휘발 블록에 두면 매 턴 캐시를 깬다(prompt_assembly: STABLE)."""
    src = inspect.getsource(chat._build_system)
    assert "contract_text" in src


def test_no_contract_means_prompt_is_unchanged():
    """계약이 없는 사용자는 지금까지와 똑같아야 한다."""
    base = chat._build_system(language="ko", nickname="승민", lead=None)
    same = chat._build_system(language="ko", nickname="승민", lead=None, contract_text="")
    assert base == same


def test_render_comes_from_the_document_not_the_stored_string():
    """저장된 rendered_text를 그대로 쓰면 옛 template의 방어를 못 받는다."""
    src = inspect.getsource(contract_repo.published_text)
    assert "render_document" in src


def test_document_hash_is_stable_across_processes():
    """Python hash()는 PYTHONHASHSEED 때문에 프로세스마다 달라진다.

    그러면 같은 계약이 매번 다른 버전으로 보이고, '정본이 같으면 새 version을 만들지
    않는다'(6.3절)는 판단이 통째로 무너진다.
    """
    doc = '[{"kind":"address"}]'
    assert ic.document_hash(doc) == ic.document_hash(doc)
    assert len(ic.document_hash(doc)) == 64
    # docstring이 Python hash()를 언급하므로 **실행 코드만** 본다.
    import ast

    tree = ast.parse(inspect.getsource(ic.document_hash).lstrip())
    fn = tree.body[0]
    body = [n for n in fn.body if not (isinstance(n, ast.Expr)
                                       and isinstance(n.value, ast.Constant))]
    code = "\n".join(ast.unparse(n) for n in body)
    assert "sha256" in code
    assert "hash(" not in code, "Python 내장 hash를 쓰고 있다"


def test_backfill_no_longer_uses_python_hash():
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "scripts" / "backfill_interaction_contracts.py").read_text()
    assert "abs(hash(" not in src


# ── 발행 정책 ────────────────────────────────────────────────

def test_high_impact_items_are_not_auto_published():
    """경계·관계 정의를 캐피 추측으로 확정하면 하지 않은 약속에 묶인다(6.3절)."""
    assert ic.Kind.TOPIC_BOUNDARY in contract_jobs.HIGH_IMPACT
    assert ic.Kind.RELATIONSHIP_DEFINITION in contract_jobs.HIGH_IMPACT


def test_ordinary_style_requests_are_auto_published():
    """매번 '이렇게 해줄까?'로 확인하면 합의가 또 사라진다(6.3절)."""
    assert ic.Kind.ADDRESS not in contract_jobs.HIGH_IMPACT
    assert ic.Kind.RESPONSE_STYLE not in contract_jobs.HIGH_IMPACT


def test_publish_closes_the_old_one_instead_of_deleting():
    """지우면 변경 이력과 rollback이 사라진다(6.3절)."""
    src = inspect.getsource(contract_repo.publish)
    assert "superseded" in src
    assert "DELETE" not in src.upper()


def test_same_document_does_not_create_a_new_version():
    src = inspect.getsource(contract_jobs.handle_contract_compile)
    assert "has_same_document" in src


def test_job_is_registered_and_reachable():
    consumer._register_handlers()
    assert consumer._REGISTRY.get(contract_jobs.JOB_CONTRACT_COMPILE) is not None


@pytest.mark.parametrize("kind", list(ic.Kind))
def test_every_kind_maps_to_a_schema_section(kind):
    """빠진 kind가 있으면 저장 시점에 KeyError로 터진다."""
    assert kind in contract_jobs._SECTION
