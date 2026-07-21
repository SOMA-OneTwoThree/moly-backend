"""출력 부호 정제 — 캐피 톤 화이트리스트(마침표·쉼표·물음표·느낌표) 밖 기호 제거. 채팅·일기 공용.

말줄임표·마크다운 강조(**,_,`)·대시(—,–,-)·물결·해시를 지운다. 이름 placeholder 토큰
(`{유저이름}`)의 중괄호·한글은 대상이 아니라 안전하다.
"""
from __future__ import annotations

import re

# 깨진·투명·제어문자 — 뜻이 없어 결정적으로 제거(공백 아닌 ""으로 지워 앞뒤 글자를 재결합).
# 치환문자(�)는 드물게 LLM/인코딩에서 새는 깨짐이라 여기서 잡는다(메�뉴 → 메뉴).
# \t\n\r는 제외 — 공백 정규화(_WS)가 처리한다. NBSP 등 유니코드 공백도 _WS(\s)가 단일 공백으로.
JUNK = re.compile(
    "["
    "�"                      # U+FFFD 치환문자(깨짐)
    "​-‏"               # 제로폭(ZWSP/ZWNJ/ZWJ) + 방향표시(LRM/RLM)
    "‪-‮"               # bidi embedding/override
    "⁠⁦-⁩"         # word-joiner + bidi isolate
    "﻿"                      # BOM
    "\x00-\x08\x0b\x0c\x0e-\x1f"  # C0 제어문자(\t\n\r 제외)
    "]"
)
ELLIPSIS = re.compile(r"\.{2,}|…+")            # ".." "..." / "…" (한 글자여도 말줄임표)
STRAY = re.compile(r"[*_`~#—–\-]+")            # 마크다운(**,_,`)·대시·물결·해시 — 부호 화이트리스트 밖
_WS = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([?!.,])")


def strip_symbols(text: str) -> str:
    """깨진/투명/제어문자 + 말줄임표·마크다운·대시류 제거 + 공백 정규화. 채팅·일기 공용."""
    if not text:
        return text
    out = JUNK.sub("", text)      # 깨짐(�)·제로폭·BOM·제어 제거(재결합 위해 "")
    out = ELLIPSIS.sub(" ", out)
    out = STRAY.sub(" ", out)
    out = _WS.sub(" ", out)
    return _SPACE_BEFORE_PUNCT.sub(r"\1", out).strip()
