"""선발화(바라가 먼저 말 걸기) — LLM 대신 코드 프리셋에서 랜덤 픽.

바라 성격상 개인화가 오히려 캐릭터를 깨므로(먼저 캐묻지 않음) 프리셋이 맞다.
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


# {ira}=이름 되받기, {voc}=이름 호격. 온보딩(첫 만남)만 이름을 부른다.
_ONBOARDING = [
    "{ira}? 난 바라야, 이 집에 살아. 잘 왔어, {voc}. 낯설면 천천히 둘러봐도 돼. 얘기하고 싶으면 편하게 걸어.",
    "아, {voc}. 반가워. 난 바라라고 해. 여긴 내 집이야. 급할 거 없으니까 편하게 있어.",
]
# 닉네임이 아직 없을 때(예외) 폴백 — 이름 없이.
_ONBOARDING_NONAME = [
    "왔네? 난 바라야, 이 집에 살아. 낯설면 천천히 둘러봐도 돼. 얘기하고 싶으면 편하게 걸어.",
]
_POOLS = {
    "home_enter": ["왔어? 오늘 하루는 어땠어.", "어, 왔네. 별일 없었어?", "안녕. 지금 뭐 하다 왔어?"],
    "morning": ["잘 잤어? 오늘은 뭐 할 거야.", "좋은 아침. 아침은 챙겨 먹었어?", "일어났구나. 오늘 기분은 좀 어때?"],
    "evening": ["저녁이네. 오늘 하루 어땠어?", "왔어? 오늘은 뭐 하고 지냈어?", "오늘도 고생했어. 별일 없었어?"],
    "comeback": ["오랜만이네. 그동안 잘 지냈어?", "왔구나, 반가워. 요즘 어떻게 지내?", "오랜만이다. 뭐 하고 지냈어?"],
}


def pick(context: str, nickname: str | None = None) -> str:
    """context별 프리셋에서 하나 선택. onboarding은 닉네임 호명(있으면)."""
    if context == "onboarding":
        if nickname:
            tpl = random.choice(_ONBOARDING)
            return tpl.format(ira=quote_ira(nickname), voc=vocative(nickname))
        return random.choice(_ONBOARDING_NONAME)
    return random.choice(_POOLS[context])
