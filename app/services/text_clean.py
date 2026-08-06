"""출력 부호 정제 — 캐피 톤 화이트리스트(마침표·쉼표·물음표·느낌표) 밖 기호 제거. 채팅·일기 공용.

말줄임표·마크다운 강조(**,_,`)·대시(—,–,-)·물결·해시를 지운다. 이름 placeholder 토큰
(`{유저이름}`)의 중괄호·한글은 대상이 아니라 안전하다.
"""
from __future__ import annotations

import re
import unicodedata

from app.services import i18n

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

# 응답에 나와도 되는 '글자 계열'을 언어마다 적는다. 여기 없는 계열의 글자는 이물질로 보고 지운다.
#
# 예전에는 반대로 지울 계열(한자·가나)만 나열했다. 그런데 유니코드 문자 계열은 150종이 넘어서
# 그 목록은 완성될 수 없다. 실제로 목록에 없던 그리스·구자라트·키릴 글자가 그대로 새어 나갔다
# (dev 캐피 발화 178건 중 4건). 그래서 '지울 것'이 아니라 '남길 것'을 적는 쪽으로 뒤집었다.
# 새로운 계열이 나와도 목록을 고칠 일이 없다.
#
# 왜 이런 글자가 나오나: 모델이 문장을 다 쓰고 멈춰야 할 자리에서 드물게 엉뚱한 언어의 토큰을
# 뽑는다. 실측 4건은 모두 완결된 문장 뒤에 꼬리처럼 붙었고 단어 중간에 끼어든 경우는 없었다.
# 그래서 지워도 본문이 깨지지 않는다. 프롬프트로도 막고 있지만 확률이라 0이 되지는 않는다.
#
# 어느 언어에서나 통과시키는 계열 — 악센트가 붙은 라틴 글자(café jalapeño)와 그 악센트 기호.
# 상표 고유명사가 그대로 나오는 게 정상이다. 영문(ASCII)은 아래 `_is_foreign`이 먼저 통과시킨다.
# 전각 라틴(\uff21~)도 넣는다 — 일본어 글에 흔하고, 빼면 멀쩡한 글자를 지운다.
_COMMON_LETTERS = "\u00c0-\u024f\u0300-\u036f\uff21-\uff3a\uff41-\uff5a"
_ALLOWED_LETTERS = {
    # 한글 = 완성형 음절 + 호환 자모 + 조합용 자모. 조합용(\u1100~)까지 넣는 이유는 자소가 분리된
    # 형태로 들어와도 한글이 통째로 지워지지 않게 하기 위해서다.
    "ko": "\uac00-\ud7a3\u3131-\u318e\u1100-\u11ff",
    # 가나 + 한자(기본 확장A 호환 확장B) + 탁점(\u3099~) — 탁점은 분리된 가나의 정상 구성요소다.
    "ja": r"぀-ヿ㐀-䶿一-鿿豈-﫿\U00020000-\U0002FA1F" "\u3099-\u309a\uff66-\uff9f",  # 끝은 반각 가타카나
    "en": "",  # 라틴뿐 — 위 공통 계열로 충분하다
}
_ALLOWED_RE = {
    lang: re.compile(f"[{_COMMON_LETTERS}{extra}]") for lang, extra in _ALLOWED_LETTERS.items()
}


def _is_foreign(ch: str, allowed: re.Pattern[str]) -> bool:
    """이 글자가 응답 언어에 없어야 할 계열인가.

    판정 대상은 글자와 결합기호뿐이다. 숫자 문장부호 이모지 공백은 여기 소관이 아니라
    STRAY JUNK가 맡는다. 결합기호까지 보는 이유는 인도계 문자의 모음기호처럼 유니코드가 글자로
    분류하지 않는 조각이 있어서다 — 글자만 지우면 그 조각이 남아 더 이상해진다(실측 확인).
    영문(ASCII)은 어느 언어에서도 통과시킨다.
    """
    if ch.isascii() or not (ch.isalpha() or unicodedata.category(ch).startswith("M")):
        return False
    return allowed.match(ch) is None


def _segments(text: str, keep: str | None) -> list[str]:
    """`keep`(닉네임)으로 끊어 판정에서 통째로 뺀다.

    닉네임은 유저가 정한 값이라 응답 언어와 계열이 다를 수 있다. 한국어로 쓰는 유저가 이름을
    키릴 문자로 지어 두면 그 이름이 이물질로 걸려 통째로 지워진다. 이름이 사라진 답을 내보내는
    쪽이 이물질이 남는 것보다 나쁘다.
    """
    return text.split(keep) if keep else [text]


def _filter_for(language: str | None) -> tuple[bool, re.Pattern[str] | None]:
    """(거를지, 허용 패턴). 목록에 없는 언어는 아예 거르지 않는다.

    새 언어를 SUPPORTED에 넣고 여기 허용 계열을 안 적으면, 거르는 쪽을 택했을 때 그 언어 본문이
    통째로 지워진다. 이물질이 남는 것보다 본문이 사라지는 게 훨씬 나쁘므로 그냥 통과시킨다.
    """
    lang = i18n.resolve(language)
    if lang not in _ALLOWED_RE:
        return False, None
    return True, _ALLOWED_RE[lang]


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


def has_foreign(text: str | None, *, language: str | None, keep: str | None = None) -> bool:
    """응답 언어에 없어야 할 계열의 글자가 섞였는지. `keep`(닉네임)은 판정에서 뺀다."""
    if not text:
        return False
    active, allowed = _filter_for(language)
    if not active:
        return False
    return any(_is_foreign(ch, allowed) for seg in _segments(text, keep) for ch in seg)


def strip_foreign(text: str | None, *, language: str | None, keep: str | None = None) -> str | None:
    """응답 언어에 없어야 할 계열의 글자를 지우고 공백을 정규화한다. `keep`(닉네임)은 그대로 둔다.

    실측된 경우는 전부 문장 뒤에 붙은 꼬리라 지워도 본문이 안 깨진다. 단어 중간에 끼어드는
    경우(예: '中국'→'국')는 아직 관측된 적이 없다 — 그런 사례가 실제로 생기는지 보려고 호출측이
    발동 사실을 로그로 남긴다. 생기면 그때 복원 방식을 다시 판단한다.
    """
    if not text:
        return text
    active, allowed = _filter_for(language)
    if not active:
        return text
    kept = [
        "".join(ch for ch in seg if not _is_foreign(ch, allowed)) for seg in _segments(text, keep)
    ]
    return _WS.sub(" ", (keep or "").join(kept)).strip()


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


# 한국어 응답 **끝**에 공백 없이 딱 붙은 라틴 조각 제거. 위 `strip_leading_meta`(응답 앞)의 짝이다.
#
# 라틴은 어느 언어에서도 정상 글자라 위 허용 목록으로는 못 잡는다 — 상표·고유명사를 지우면
# 안 되니 통과시키는 게 맞다. 그래서 위치가 아니라 **붙어 있는 방식**으로 가른다.
#
# dev 한국어 발화 189건에서 라틴은 2건뿐이었다.
#   "...깨어났네.arsuarmi"  — 마침표에 공백 없이 붙음. 정상적으로 쓴 글에는 이런 모양이 없다.
#   "...모르겠네. Adios"    — 공백으로 띄어짐. 정상적인 영어 끝맺음과 구조가 같다.
# 뒤엣것까지 지우려면 "마지막 한글 뒤 라틴은 전부 이물질"이라는 규칙이 필요한데, 189건은
# 개발용 계정 대화라 실제 사용자 표본이 아니다. 실사용에서는 영어로 끝나는 답이 얼마든지
# 나올 수 있으므로 그 규칙은 세우지 않는다. 앞엣것만 지우고, 나머지는 로그로 쌓아 실제
# 데이터가 모인 뒤에 다시 판단한다.
#
# 앞부분은 마지막 한글 바로 뒤의 문장부호까지만 허용한다(".arsuarmi"). 공백이 하나라도
# 끼면 발동하지 않는다.
_TRAILING_ATTACHED_LATIN = re.compile(r"^([^\w\s]*)([A-Za-z]+)$")


def strip_trailing_latin(text: str) -> str:
    """한국어 응답 끝에 공백 없이 붙은 라틴 조각을 지운다. 문장부호는 남긴다.

    한글이 하나도 없으면 그대로 둔다(본문을 통째로 지우는 사고 방지). 공백으로 띄어진
    라틴은 정상 끝맺음과 구분할 수 없으므로 건드리지 않는다.
    """
    if not text:
        return text
    last = None
    for last in _HANGUL.finditer(text):
        pass
    if last is None:
        return text  # 한글 없음 — 호출측이 ko로 게이팅하지만 방어적으로 둔다
    m = _TRAILING_ATTACHED_LATIN.match(text[last.end():])
    if m is None:
        return text
    return (text[: last.end()] + m.group(1)).strip() or text
