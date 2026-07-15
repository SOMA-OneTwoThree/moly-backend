"""선발화(캐피가 먼저 말 걸기) — LLM 대신 코드 프리셋에서 랜덤 픽.

캐피 성격상 개인화가 오히려 캐릭터를 깨므로(먼저 캐묻지 않음) 프리셋이 맞다.
비용·지연 0, 톤 완전 통제. 온보딩만 유저 닉네임을 부르며, 한글 받침에 맞춰
호격(아/야)·되받기(이라고/라고) 조사를 자동으로 붙인다.
"""
from __future__ import annotations

import random

CONTEXTS = {"onboarding", "home_enter", "morning", "evening", "comeback"}


def vocative(name: str) -> str:
    """이름 호격 — 받침 있으면 '아', 없으면 '야'. 비한글은 이름 그대로."""
    if not name:
        return ""
    last = name[-1]
    if "가" <= last <= "힣":
        return name + ("아" if (ord(last) - 0xAC00) % 28 else "야")
    return name


def quote_ira(name: str) -> str:
    """이름 되받기 — 받침 있으면 '이라고', 없으면 '라고'. 비한글은 '라고'."""
    if not name:
        return ""
    last = name[-1]
    if "가" <= last <= "힣":
        return name + ("이라고" if (ord(last) - 0xAC00) % 28 else "라고")
    return name + "라고"


def copula(name: str) -> str:
    """서술격 조사 — 받침 있으면 '이야', 없으면 '야'. ("이름은 '지훈'이야" / "'지호'야")

    프롬프트에 이름을 박을 때 쓴다. 지시문 자체가 조사를 틀리면 캐피도 따라 틀린다.
    """
    if not name:
        return ""
    last = name[-1]
    if "가" <= last <= "힣":
        return name + ("이야" if (ord(last) - 0xAC00) % 28 else "야")
    return name + "야"


def with_wa(name: str) -> str:
    """동반 조사 — 받침 있으면 '과', 없으면 '와'. ("승민과" / "지호와")"""
    if not name:
        return ""
    last = name[-1]
    if "가" <= last <= "힣":
        return name + ("과" if (ord(last) - 0xAC00) % 28 else "와")
    return name + "와"


def subject(name: str) -> str:
    """주격 조사 — 받침 있으면 '이', 없으면 '가'. ("승민이" / "지호가")"""
    if not name:
        return ""
    last = name[-1]
    if "가" <= last <= "힣":
        return name + ("이" if (ord(last) - 0xAC00) % 28 else "가")
    return name


# {ira}=이름 되받기, {voc}=이름 호격. 온보딩(첫 만남)은 닉네임을 부른다(온보딩 후라 항상 있음).
_ONBOARDING = [
    "{ira}? 난 캐피야, 이 집에 살아. 잘 왔어, {voc}. 낯설면 천천히 둘러봐도 돼. 얘기하고 싶으면 편하게 걸어.",
    "아, {voc}. 반가워. 난 캐피라고 해. 여긴 내 집이야. 급할 거 없으니까 편하게 있어.",
]
# home_enter는 유저가 그날 처음 들어온 시각에 맞춰 고른다(하루 1회 발급이라 버킷도 1회 확정).
# {subj}=주격 이름(승민이/지호가) 치환.
_HOME_BY_TIME = {
    "dawn": [
        "이 시간에 깨어 있네. 잠이 안 와?",
        "아직 안 잤구나. 나도 방금 눈 떴어.",
        "새벽인데 깨어있네? 나한테 해줄만한 재밌는 얘기 있어?",
    ],
    "morning": [
        "잘 잤어? 배고프다... 아침으로 뭐 먹을까?",
        "좋은 아침. 창밖이 환하다.",
        "일어났구나. 나도 방금 기지개 켰어.",
        "벌써 아침이네, 오늘 날씨는 어때?",
        "{subj} 왔어? 아침은?",
    ],
    "day": [
        "어, 왔네. 나는 소파에 늘어져 있었어.",
        "한낮이다. 밖은 좀 어때?",
        "방금 낮잠에서 깼어. 잘 왔다.",
        "안녕. 나는 창밖 구경하고 있었어.",
        "이 시간에 오는 건 오랜만이네.",
    ],
    "evening": [
        "벌써 저녁이네. 오늘 하루는 어땠어?",
        "왔어? 나는 좀 전에 밥 먹고 조금 걷다가 왔어.",
        "오늘도 고생 많았어. 오늘은 어땠어?",
        "오늘도 고생했어. 별일 없었어?",
    ],
    "night": [
        "늦었는데 아직 안자? 자기 전에 오늘 있었던 일 얘기해줄래?",
        "내가 제일 좋아하는 LP 하나 틀어놨어, 같이 들으면서 얘기하자.",
        "늦게 왔네. 피곤하지? 오늘은 좀 어땠어?",
    ],
}
_POOLS = {
    "morning": [
        "잘 잤어? 오늘은 뭐 할 거야.",
        "좋은 아침. 아침은 챙겨 먹었어?",
        "일어났구나. 오늘 기분은 좀 어때?",
        "아침이야. 나는 아직 이불 속이었어.",
        "왔네. 오늘 하루도 천천히 가자.",
    ],
    "evening": [
        "벌써 저녁이네. 오늘 하루는 어땠어?",
        "왔어? 오늘은 뭐 하고 지냈어?",
        "오늘도 고생했어. 별일 없었어?",
        "오늘도 고생 많았어. 오늘은 어땠어?",
        "하루 끝났네. 나는 창 열어두고 있었어.",
        "저녁이다. 나는 방금 밥 먹고 왔어.",
    ],
    "comeback": [
        "오랜만이네. 그동안 잘 지냈어? 밀린 얘기가 많아.",
        "드디어 왔구나. 반가워. 쌓인 얘기 좀 해줘. 궁금한게 많아.",
        "오랜만이다. 한동안 뭐 하고 지냈어?",
        "한참 안보여서 걱정했어. 그래도 오랜만에 얼굴 보니 좋다. 그동안 별일 없었어?",
    ],
}


def time_bucket(hour: int) -> str:
    """로컬 시각 → home_enter 인사 버킷. 하루 경계(04:00)와 맞춰 새벽부터 시작한다."""
    if 4 <= hour <= 6:
        return "dawn"
    if 7 <= hour <= 10:
        return "morning"
    if 11 <= hour <= 16:
        return "day"
    if 17 <= hour <= 20:
        return "evening"
    return "night"  # 21~03


def _personalize(tpl: str, nickname: str | None) -> str:
    """{subj}=주격 이름, {voc}=호격 이름 치환. 닉네임 없으면 '너'로(온보딩 후라 보통 있음)."""
    n = nickname or "너"
    return tpl.format(subj=subject(nickname) or "너", voc=vocative(n), name=n)


def pick(context: str, nickname: str | None = None, hour: int | None = None) -> str:
    """context별 프리셋에서 하나 선택. home_enter만 시각에 따라 풀이 갈린다(hour 없으면 낮).

    이름 자리가 있는 문구(예: '{subj} 왔어?')는 닉네임으로 치환한다.
    """
    if context == "onboarding":
        if not nickname:  # 온보딩은 닉네임 확정 후라 정상 경로엔 안 오지만, 안전 폴백.
            return "난 캐피야, 이 집에 살아. 편하게 얘기 걸어."
        tpl = random.choice(_ONBOARDING)
        return tpl.format(ira=quote_ira(nickname), voc=vocative(nickname))
    if context == "home_enter":
        pool = _HOME_BY_TIME[time_bucket(hour if hour is not None else 12)]
    else:
        pool = _POOLS[context]
    tpl = random.choice(pool)
    return _personalize(tpl, nickname) if "{" in tpl else tpl
