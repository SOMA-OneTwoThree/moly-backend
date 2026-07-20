"""출력 부호 정제 — 캐피 톤 화이트리스트(마침표·쉼표·물음표·느낌표) 밖 기호 제거. 채팅·일기 공용.

말줄임표·마크다운 강조(**,_,`)·대시(—,–,-)·물결·해시를 지운다. 이름 placeholder 토큰
(`{유저이름}`)의 중괄호·한글은 대상이 아니라 안전하다.
"""
from __future__ import annotations

import re

ELLIPSIS = re.compile(r"\.{2,}|…+")            # ".." "..." / "…" (한 글자여도 말줄임표)
STRAY = re.compile(r"[*_`~#—–\-]+")            # 마크다운(**,_,`)·대시·물결·해시 — 부호 화이트리스트 밖
_WS = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([?!.,])")


def strip_symbols(text: str) -> str:
    """말줄임표·마크다운·대시류 제거 + 공백 정규화."""
    if not text:
        return text
    out = ELLIPSIS.sub(" ", text)
    out = STRAY.sub(" ", out)
    out = _WS.sub(" ", out)
    return _SPACE_BEFORE_PUNCT.sub(r"\1", out).strip()
