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

# 한국어 응답에 섞이면 안 되는 문자 = 한자(기본·확장A·호환·확장B astral) + 일본어 가나.
# LLM이 드물게 한글 대신 CJK 토큰을 뱉는 아티팩트 탐지용(단어가 깨지므로 삭제 아닌 재작성으로 복원).
# 라틴·숫자·이모지는 대상 아님(이모지는 STRAY/별도 처리, 여기선 '다른 언어 글자'만).
_FOREIGN_KO = re.compile(
    r"[぀-ヿ㐀-䶿一-鿿豈-﫿\U00020000-\U0002FA1F]"
)


def strip_symbols(text: str) -> str:
    """말줄임표·마크다운·대시류 제거 + 공백 정규화."""
    if not text:
        return text
    out = ELLIPSIS.sub(" ", text)
    out = STRAY.sub(" ", out)
    out = _WS.sub(" ", out)
    return _SPACE_BEFORE_PUNCT.sub(r"\1", out).strip()


def has_foreign_ko(text: str) -> bool:
    """한국어 응답에 섞이면 안 되는 문자(한자·가나) 존재 여부. 삭제 대신 재작성 복원 트리거용."""
    return bool(text) and _FOREIGN_KO.search(text) is not None


def strip_foreign_ko(text: str) -> str:
    """최후수단 — 외래문자(한자·가나) 제거 + 공백 정규화. 재작성 복원까지 실패했을 때만.

    단어가 깨질 수 있어(예: '中국'→'국') 마지막 안전망으로만 쓴다. 중국어가 그대로 나가는 것보단 낫다.
    """
    if not text:
        return text
    return _WS.sub(" ", _FOREIGN_KO.sub("", text)).strip()
