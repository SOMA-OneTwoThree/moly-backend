"""닉네임 플레이스홀더 — 저장·기억은 placeholder, egress·LLM 투입은 현재 이름 렌더.

불변식: 유저 실제 이름을 DB 본문(messages·greetings·diaries·chat_contexts)에 문자열로 저장하지
않는다. 영속 텍스트는 `{name...}` 토큰으로만 두고, 이름은 **사용 시점에** 현재 `profiles.nickname`으로
렌더한다. 렌더가 늘 최신이라 이 DB 표면들에선 개명 드리프트가 구조적으로 불가능하다.

단, mem0(장기기억)는 예외다: 추출 품질을 위해 렌더된 실명 텍스트를 넣고(M2), 이름이 안 남는 건
추출 LLM이 `memory.py`의 `custom_instructions`("이름 저장 금지")를 지키는 데 의존하는 **best-effort**다.

- `to_placeholder(text, nickname)` — 저장 직전. 현재 이름+조사 → 토큰. 멱등.
- `render(text, nickname)` — 클라 응답/LLM 프롬프트·mem0 투입 직전. 토큰 → 현재 이름+조사.

조사는 두 부류다. 받침에 따라 갈리는 조사(승민이/지호가)는 타입 토큰(`{name:subj}` 등)으로
인코딩하고, 받침 무관 조사(의·도·만·에게·한테·처럼…)는 `{name}` + 리터럴 조사로 둔다.
어느 쪽이든 to_placeholder가 실명+조사 표면형을 전부 열거해 역치환하므로 리터럴 실명이 안 남는다.
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


def _with_rang(name: str) -> str:
    """접속·동반 (이)랑 — 받침 있으면 '이랑', 없으면 '랑'. 비한글은 '랑'."""
    if not name:
        return ""
    last = name[-1]
    if "가" <= last <= "힣":
        return name + ("이랑" if (ord(last) - 0xAC00) % 28 else "랑")
    return name + "랑"


def _with_na(name: str) -> str:
    """선택 (이)나 — 받침 있으면 '이나', 없으면 '나'. 비한글은 '나'."""
    if not name:
        return ""
    last = name[-1]
    if "가" <= last <= "힣":
        return name + ("이나" if (ord(last) - 0xAC00) % 28 else "나")
    return name + "나"


def _with_rado(name: str) -> str:
    """양보 (이)라도 — 받침 있으면 '이라도', 없으면 '라도'. 비한글은 '라도'."""
    if not name:
        return ""
    last = name[-1]
    if "가" <= last <= "힣":
        return name + ("이라도" if (ord(last) - 0xAC00) % 28 else "라도")
    return name + "라도"


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
    "rang": _with_rang,             # (이)랑
    "ina": _with_na,                # (이)나
    "irado": _with_rado,            # (이)라도
}

# 접두 공유 키(ira ⊂ irado)는 긴 것 먼저 — 뒤 `\}` 강제로 백트래킹되지만 방어적으로 정렬.
_TOKEN = re.compile(r"\{name(?::(voc|subj|cop|wa|irado|ira|rang|ina|top|obj|ro))?\}")


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
    ("rang", _with_rang),
    ("ina", _with_na),
    ("irado", _with_rado),
)

# 받침 무관 조사 — 이름에 그대로 붙는다(과치환은 조사 문자열이 앵커 + 뒤 경계검사로 차단).
# 표면형 = 이름+조사, 치환 = `{name}`+리터럴 조사(render 시 받침 무관이라 항상 정확).
# 긴 조사 먼저 열거(만큼 > 만, 에게서 > 에게)해 부분 매칭을 막는다(실 매칭은 아래서 길이 정렬).
_BATCHIM_FREE_PARTICLES = (
    "에게서", "한테서", "에게", "한테", "께서", "께",
    "처럼", "보다", "만큼", "밖에", "부터", "까지",
    "마다", "조차", "마저", "마는", "도", "만", "의",
)


def _surface_map(nickname: str) -> dict[str, str]:
    """현재 이름의 조사 표면형 → 치환 문자열. 표면형이 이름과 같거나(조사 없음) 중복이면 제외.

    받침 무관 조사는 흔한 단어 조각(수'도'·수'만'·수'의')과 겹치므로 1음절 이름에는 붙이지 않는다
    (bare 매칭 금지와 같은 이유). 받침 의존 조사는 조사가 앵커라 1음절도 비교적 안전해 유지한다.
    """
    out: dict[str, str] = {}
    for key, fn in _FORMS:
        surface = fn(nickname)
        if surface and surface != nickname and surface not in out:
            out[surface] = "{name:%s}" % key
    if len(nickname) > 1:  # 1음절 이름은 받침 무관 조사 표면형 제외(과치환 억제)
        for particle in _BATCHIM_FREE_PARTICLES:
            out.setdefault(nickname + particle, "{name}" + particle)
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
