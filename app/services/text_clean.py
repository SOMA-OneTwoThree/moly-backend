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
    "\ufffd"                      # U+FFFD 치환문자(깨짐)
    "\u200b-\u200f"              # 제로폭(ZWSP/ZWNJ/ZWJ) + 방향표시(LRM/RLM)
    "\u202a-\u202e"              # bidi embedding/override
    "\u2060\u2066-\u2069"       # word-joiner + bidi isolate
    "\ufeff"                      # BOM
    "\x00-\x08\x0b\x0c\x0e-\x1f"  # C0 제어문자(\t\n\r 제외)
    "]"
)
ELLIPSIS = re.compile(r"\.{2,}|…+")            # ".." "..." / "…" (한 글자여도 말줄임표)
STRAY = re.compile(r"[*_`~#—–\-]+")            # 마크다운(**,_,`)·대시·물결·해시 — 부호 화이트리스트 밖
_STRAY_KEEP_HYPHEN = re.compile(r"[*_`~#—–]+")  # ASCII 하이픈(-) 유지 — 영어 자연 부호(laid-back)
_WS = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([?!.,])")

# 한국어 응답에 섞이면 안 되는 문자 = 한자(기본·확장A·호환·확장B astral) + 일본어 가나.
# LLM이 드물게 한글 대신 CJK 토큰을 뱉는 아티팩트 탐지용(단어가 깨지므로 삭제 아닌 재작성으로 복원).
# 라틴·숫자·이모지는 대상 아님(이모지는 STRAY/별도 처리, 여기선 '다른 언어 글자'만).
_FOREIGN_KO = re.compile(
    r"[぀-ヿ㐀-䶿一-鿿豈-﫿\U00020000-\U0002FA1F]"
)


def strip_symbols(text: str, *, keep_hyphen: bool = False) -> str:
    """깨진/투명/제어문자 + 말줄임표·마크다운·대시류 제거 + 공백 정규화. 채팅·일기 공용.

    keep_hyphen=True면 ASCII 하이픈(-)을 남긴다(영어 응답의 자연 부호 laid-back 보존). 기본(ko)은 제거.
    """
    if not text:
        return text
    out = JUNK.sub("", text)      # 깨짐(�)·제로폭·BOM·제어 제거(재결합 위해 "")
    out = ELLIPSIS.sub(" ", out)
    out = (_STRAY_KEEP_HYPHEN if keep_hyphen else STRAY).sub(" ", out)
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


# 메타 프리앰블 탐지/제거 — 모델이 한국어 응답 앞에 자기 판단·방침을 라틴 문자 '문장'으로 흘리는
# 아티팩트(SOMA-329). 실측 2건: 영어("momentary pause here this is a serious moment…" + 한국어),
# 스페인어("Ha pillado ese mensaje… Te contesto en mi papel normal. + 한국어"). 한글이 응답의 진짜
# 본문이므로 '첫 한글 앞에 붙은 긴 라틴 런'만 제거한다. 구조적 탐지라 언어 불문 일반화됨.
_HANGUL = re.compile(r"[가-힣]")
_EN_WORD = re.compile(r"[A-Za-z]+")
_META_HEAD_PUNCT = re.compile(r"[.:]")  # 메타는 완결된 문장(마침표·콜론) — 제목·가사와 구분
_META_MIN_WORDS = 4  # 첫 한글 앞 라틴 단어가 이 이상이면 '문장 분량'으로 봄(짧은 토큰 LP·AI·BTS는 미발동)


def strip_leading_meta(text: str, *, min_words: int = _META_MIN_WORDS) -> str:
    """한국어 응답 앞에 새어나온 라틴 메타 프리앰블만 결정적으로 벗긴다(첫 한글부터 남김).

    발동 조건(전부 만족): ① 한글 존재(제거 후 남을 본문 있음) ② 첫 글자가 한글 아님 ③ 첫 한글 앞
    라틴 단어 수 >= min_words ④ 그 앞부분에 마침표·콜론이 있음(완결 문장). ③+④로 '메타 문장'과
    영어 제목·가사(구두점 없는 라틴 런, 예 "Red Velvet Special Clip 봤어?")를 구분해 오탐을 막는다.
    호출측에서 language=='ko' 게이팅. LLM 호출 없는 순수 정규식이라 지연·비용 0.
    프로덕션 캐피 응답 3071건 검증: 실측 유출 2건(영어·스페인어, 둘 다 마침표 문장)만 변경, 오탐 0건.
    """
    if not text:
        return text
    m = _HANGUL.search(text)
    if m is None or m.start() == 0:
        return text  # 한글 없음(전량삭제 방지) or 이미 한글로 시작(정상)
    head = text[: m.start()]
    if len(_EN_WORD.findall(head)) < min_words or _META_HEAD_PUNCT.search(head) is None:
        return text  # 짧은 토큰/고유명사 or 구두점 없는 제목·가사 → 미발동
    body = text[m.start():]  # 첫 한글부터 = 페르소나 본문(한글로 시작하므로 lstrip 불필요)
    return body or text  # 방어적 fail-safe — 본문이 비면 원문 유지
