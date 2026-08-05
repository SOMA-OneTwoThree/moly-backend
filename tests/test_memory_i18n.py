"""다국어 — 영어 사용자에게 한국어가 새지 않는다.

감사 지적: 영어 `Hello`로 회상했을 때 헤더가 `[기억]`이었고 어긋나는 기억 안내문도 한국어
였다. 서버가 만든 한국어 지시문이 프롬프트에 들어가면 모델이 그 언어로 답할 수 있다.

**정본은 locale-neutral이고 언어별은 투영일 뿐이다** — 기억 본문 자체가 한국어인 건 별개
문제(원문이 한국어라 그렇다)이고, 여기서 막는 건 **서버가 만든 문구**다.
"""
from __future__ import annotations

import uuid

import pytest

from app.services import interaction_contract as ic
from app.services import mem0_recall as mr


def _r(status="active"):
    return mr.Recalled("something", status, 0.3, None, uuid.uuid4() if status == "ambiguous" else None)


@pytest.mark.parametrize("lang,head", [("ko", "[기억]"), ("en", "[memory]"), ("ja", "[記憶]")])
def test_memory_header_follows_locale(lang, head):
    assert mr.render_block([_r()], language=lang).startswith(head)


def test_english_user_gets_no_korean_header():
    """감사가 실제로 잡은 것 — en인데 [기억]이 나왔다."""
    assert "[기억]" not in mr.render_block([_r()], language="en")


@pytest.mark.parametrize("lang", ["ko", "en", "ja"])
def test_uncertainty_notice_exists_in_every_language(lang):
    """빠진 언어가 있으면 그 사용자에게만 한국어 지시문이 간다."""
    out = mr.render_block([_r("ambiguous")], language=lang)
    assert out and mr._UNCERTAIN[lang][:6] in out


def test_unknown_locale_falls_back_to_korean_not_empty():
    """모르는 언어에 빈 문자열을 주면 그 사용자는 기억을 아예 못 받는다."""
    assert mr.render_block([_r()], language="fr")


# ── 계약 렌더 ────────────────────────────────────────────────

def _d():
    return ic.Directive(
        kind=ic.Kind.ADDRESS, action=ic.Action.USE, condition=ic.Condition.ALWAYS,
        polarity=ic.Polarity.POSITIVE, target_literal="Sam",
    )


@pytest.mark.parametrize("lang", ["ko", "en", "ja"])
def test_contract_renders_in_the_users_language(lang):
    out = ic.render_document([_d()], language=lang)
    assert out and "Sam" in out


def test_contract_header_is_not_korean_for_english():
    assert "약속" not in ic.render_document([_d()], language="en")


def test_literal_stays_quoted_in_every_language():
    """언어를 바꾸다 인용 슬롯을 잃으면 사용자 값이 명령 위치로 간다."""
    for lang in ("ko", "en", "ja"):
        assert "「Sam」" in ic.render_document([_d()], language=lang)


def test_document_is_locale_neutral():
    """정본이 언어에 묶이면 locale 변경마다 새 version이 생긴다(6.3절)."""
    doc = '[{"kind":"address","action":"use"}]'
    assert ic.document_hash(doc) == ic.document_hash(doc)
