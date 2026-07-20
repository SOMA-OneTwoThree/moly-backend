"""닉네임 플레이스홀더 — 저장·기억은 placeholder, egress·LLM 투입은 현재 이름 렌더.

불변식: 유저 실제 이름을 DB 본문·mem0·LLM 히스토리 어디에도 문자열로 저장하지 않는다.
영속 텍스트는 `{name...}` 토큰으로만 두고, 이름은 **사용 시점에** 현재 `profiles.nickname`으로
렌더한다. 렌더가 늘 최신이라 개명 드리프트가 구조적으로 불가능하다.

- `to_placeholder(text, nickname)` — 저장/mem0 투입 직전. 현재 이름+조사 → 토큰. 멱등.
- `render(text, nickname)` — 클라 응답/LLM 프롬프트 투입 직전. 토큰 → 현재 이름+조사.

조사는 받침에 따라 갈리므로(승민이/지호가) 토큰에 인코딩한다. 받침 무관 조사(의·도·만·
에게·한테·처럼…)는 `{name}` + 리터럴로 안전하다.
"""
from __future__ import annotations

import re

from app.services import greetings

# render 시 nickname이 없을 때(온보딩 전 등 정상 경로 밖) 쓰는 폴백 이름.
_FALLBACK_NAME = "너"


def _topical(name: str) -> str:
    """보조사 은/는 — 받침 있으면 '은', 없으면 '는'. 비한글은 '는'."""
    if not name:
        return ""
    last = name[-1]
    if "가" <= last <= "힣":
        return name + ("은" if (ord(last) - 0xAC00) % 28 else "는")
    return name + "는"


def _object(name: str) -> str:
    """목적격 을/를 — 받침 있으면 '을', 없으면 '를'. 비한글은 '를'."""
    if not name:
        return ""
    last = name[-1]
    if "가" <= last <= "힣":
        return name + ("을" if (ord(last) - 0xAC00) % 28 else "를")
    return name + "를"


def _directional(name: str) -> str:
    """방향격 (으)로 — 받침 없거나 ㄹ받침이면 '로', 그 외 받침은 '으로'. 비한글은 '로'."""
    if not name:
        return ""
    last = name[-1]
    if "가" <= last <= "힣":
        jong = (ord(last) - 0xAC00) % 28
        return name + ("로" if jong in (0, 8) else "으로")  # 8 = ㄹ 종성
    return name + "로"


# 토큰 키 → 조사 렌더러. bare("")는 이름 그대로.
_RENDERERS = {
    "": lambda n: n,
    "voc": greetings.vocative,      # 아/야
    "subj": greetings.subject,      # 이/가
    "cop": greetings.copula,        # 이야/야
    "wa": greetings.with_wa,        # 와/과
    "ira": greetings.quote_ira,     # (이)라고
    "top": _topical,                # 은/는
    "obj": _object,                 # 을/를
    "ro": _directional,             # (으)로
}

_TOKEN = re.compile(r"\{name(?::(voc|subj|cop|wa|ira|top|obj|ro))?\}")


def render(text: str | None, nickname: str | None) -> str | None:
    """`{name...}` 토큰을 현재 이름+조사로 치환. 토큰이 없는 옛 리터럴 텍스트는 그대로 통과.

    nickname이 없으면 폴백 이름('너')으로 렌더한다(정상 경로에선 항상 이름이 있음).
    """
    if not text:
        return text
    name = nickname or _FALLBACK_NAME
    return _TOKEN.sub(lambda m: _RENDERERS[m.group(1) or ""](name), text)


# 조사 형태 생성기 — to_placeholder가 현재 이름의 모든 표면형을 만들어 역치환한다.
# 우선순위 순(앞이 이김) — 받침 없는 이름에서 voc와 cop가 같은 '야'로 충돌할 때 voc(호명)를 택한다.
# (채팅에서 이름은 부르는 경우가 서술('~야')보다 압도적으로 흔하다.)
_FORMS = (
    ("voc", greetings.vocative),
    ("ira", greetings.quote_ira),
    ("cop", greetings.copula),
    ("wa", greetings.with_wa),
    ("subj", greetings.subject),
    ("top", _topical),
    ("obj", _object),
    ("ro", _directional),
)


def _surface_map(nickname: str) -> dict[str, str]:
    """현재 이름의 조사 표면형 → 토큰. 표면형이 이름과 같거나(조사 없음) 중복이면 제외."""
    out: dict[str, str] = {}
    for key, fn in _FORMS:
        surface = fn(nickname)
        if surface and surface != nickname and surface not in out:
            out[surface] = "{name:%s}" % key
    return out


# 경계: 앞은 한글 음절이 아님(또는 문장 시작), 뒤는 한글/영숫자가 이어지지 않음(과치환 방지).
_LEFT = r"(?<![가-힣])"
_RIGHT = r"(?![0-9A-Za-z가-힣])"


def to_placeholder(text: str | None, nickname: str | None) -> str | None:
    """현재 이름+조사를 `{name...}` 토큰으로 역치환. 멱등(이미 토큰이 있으면 그대로).

    과치환 방지: 이름 앞이 한글 음절이면(국'민'·승'민') 미매칭, 조사/이름 뒤에 한글·영숫자가
    붙으면 미매칭(승민'아빠'). 1음절 이름은 bare 단독 매칭을 금지해(수요일의 '수' 등) 오염을 막는다.
    받침 무관 조사가 이름에 바로 붙은 형태(승민'의')는 안전을 위해 미매칭(리터럴 잔존 허용).
    """
    if not text or not nickname:
        return text
    if "{name" in text:  # 멱등 — 이미 placeholder면 재치환 skip
        return text

    surfaces = _surface_map(nickname)
    if surfaces:
        # 긴 표면형 우선(승민이라고 > 승민이) — 부분 매칭 방지.
        ordered = sorted(surfaces, key=len, reverse=True)
        josa_re = re.compile(
            _LEFT + "(" + "|".join(re.escape(s) for s in ordered) + ")" + _RIGHT
        )
        text = josa_re.sub(lambda m: surfaces[m.group(1)], text)

    if len(nickname) > 1:  # 1음절 이름은 bare 매칭 금지(과치환 억제)
        bare_re = re.compile(_LEFT + re.escape(nickname) + _RIGHT)
        text = bare_re.sub("{name}", text)

    return text
