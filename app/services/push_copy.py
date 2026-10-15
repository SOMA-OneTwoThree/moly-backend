"""서버 푸시 문구 — 문구 id × 언어. DB 접근 없음(greetings.py 선례).

분기(갈래·경우 판정)는 notify.py가 소유하고, 여기는 문구만 안다. 언어는 i18n.pick
(미지원 언어는 en 폴백). 한국어는 2026-10-14 사용자가 줄 단위로 확정한 문구다
(docs/PUSH-WORK-PLAN.md 3장). 영어·일본어는 같은 id의 뜻을 현지화했다(2026-10-15, 별도 검토 두 곳의
결과를 합치고 최종 확인).

- 제목은 모든 언어에서 앱 이름 `BeCappy`(루틴 푸시만 문구별 제목).
- 저녁 대화(c.*)는 캐피가 직접 하는 말: 1인칭 반말, 쉼표·이모지 없음, ko는 마침표도 없음.
  누르면 같은 본문이 대화창 첫마디가 된다(chat.get_greeting) — 문구 id로 다시 찾는다.
- 루틴(rm.*, rr)은 앱이 하는 말: 해요체, 제목 끝 이모지 하나. 스트릭은 이미 이어 온 기록이
  깨지지 않게 하라는 뜻으로만 쓴다.
- 대화 갈래별 문구 수는 사용자 확정값 — test_push_copy._POOL_SIZES가 기준.
"""
from __future__ import annotations

import random

from app.services import i18n

TITLE = "BeCappy"

# 저녁 대화 갈래 키 — notify._category가 반환하는 값.
MORE_CHAT = "more_chat"            # 오늘(활동일) 대화함
FIRST_TOUCH = "first_touch"        # 가입 후 아직 첫 대화 전
DEFAULT_RECENT = "default_recent"  # 마지막 대화 1~2일 전
DEFAULT_MISSING = "default_missing"  # 3~6일
DEFAULT_LONG = "default_long"      # 7일+, 또는 대화 없이 가입 8일+

CATEGORIES: tuple[str, ...] = (MORE_CHAT, FIRST_TOUCH, DEFAULT_RECENT, DEFAULT_MISSING, DEFAULT_LONG)

FALLBACK = "c.fallback"  # 갈래 판단 실패 시 — 어떤 상태에도 거짓이 되지 않는 말

_CHAT: dict[str, dict[str, str]] = {
    "c.more.1": {
        "ko": "오늘 했던 얘기 조금 더 할까?",
        "en": "Want to talk a little more about today?",
        "ja": "今日の話もう少しする？",
    },
    "c.more.2": {
        "ko": "아까 하던 얘기 마저 해 줄래?",
        "en": "Can you tell me the rest of what you were saying earlier?",
        "ja": "さっきの話のつづき聞かせてくれる？",
    },
    "c.more.3": {
        "ko": "하루 끝나기 전에 한 번 더 얘기할래?",
        "en": "Want to talk one more time before the day is over?",
        "ja": "一日が終わる前にもう一回話さない？",
    },
    "c.more.4": {
        "ko": "오늘 하루는 어땠어?",
        "en": "How was your day?",
        "ja": "今日はどんな一日だった？",
    },
    "c.more.5": {
        "ko": "나랑 조금만 더 놀아 줄래?",
        "en": "Will you hang out with me a little longer?",
        "ja": "もう少しだけぼくと遊んでくれる？",
    },
    "c.more.6": {
        "ko": "오늘 저녁에 뭐 했는지 나한테도 알려줘!",
        "en": "Fill me in on what you've been up to this evening!",
        "ja": "今夜なにしてたかぼくにも教えて！",
    },
    "c.first.1": {
        "ko": "오늘 하루는 어땠어? 한마디만 해 줘도 좋아",
        "en": "How was your day? Even just one word would be nice",
        "ja": "今日はどんな一日だった？ひとことだけでもうれしいな",
    },
    "c.first.2": {
        "ko": "저녁은 먹었어? 오늘 있었던 일 나한테 들려줄래?",
        "en": "Have you had dinner yet? Want to tell me about your day?",
        "ja": "夕ごはんは食べた？今日あったことぼくに聞かせてくれる？",
    },
    "c.first.3": {
        "ko": "오늘 힘든 일은 없었어? 있었으면 나한테 털어놔도 돼",
        "en": "Did anything tough happen today? It's okay to tell me",
        "ja": "今日つらいことはなかった？あったらぼくに話していいよ",
    },
    "c.recent.1": {
        "ko": "오늘 하루는 어땠어?",
        "en": "How was your day?",
        "ja": "今日はどんな一日だった？",
    },
    "c.recent.2": {
        "ko": "오늘 하루도 고생 많았어! 힘들지는 않았어?",
        "en": "You got through another day! Was it a tough one?",
        "ja": "今日もおつかれさま！大変じゃなかった？",
    },
    "c.recent.3": {
        "ko": "오늘 힘들지는 않았어?",
        "en": "Was today hard on you?",
        "ja": "今日は大変じゃなかった？",
    },
    "c.recent.4": {
        "ko": "너의 오늘 하루는 어땠어?",
        "en": "How was today for you?",
        "ja": "きみの今日はどうだった？",
    },
    "c.recent.5": {
        "ko": "오늘 제일 기억에 남는 일 하나 들려줘",
        "en": "Tell me the one thing that stood out most today",
        "ja": "今日いちばん心に残ったことをひとつ聞かせて",
    },
    "c.missing.1": {
        "ko": "요즘은 어떻게 지내? 오랜만에 나랑 얘기하자",
        "en": "How have you been lately? Let's catch up",
        "ja": "最近どうしてる？ひさしぶりにぼくと話そう",
    },
    "c.missing.2": {
        "ko": "며칠 못 봤네 그동안 별일 없었어?",
        "en": "Haven't seen you in a few days. All good?",
        "ja": "何日か会えなかったね。変わりなかった？",
    },
    "c.missing.3": {
        "ko": "요즘 많이 바빴어? 잠깐이라도 얘기하자",
        "en": "Been really busy lately? Let's talk even just for a minute",
        "ja": "最近いそがしかった？ちょっとだけでも話そう",
    },
    "c.missing.4": {
        "ko": "오늘은 어떤 하루였어? 며칠 만에 듣고 싶어",
        "en": "What was your day like? It's been a few days and I'd love to hear about it",
        "ja": "今日はどんな一日だった？数日ぶりにきみの話を聞きたいな",
    },
    "c.missing.5": {
        "ko": "그동안 잘 지냈어? 밀린 얘기 들려줄래?",
        "en": "Been doing well? Want to tell me what I missed?",
        "ja": "元気にしてた？この数日のこと聞かせてくれる？",
    },
    "c.missing.6": {
        "ko": "요즘 무슨 일 있었어? 나한테 다 얘기해 줘",
        "en": "What's been going on lately? Tell me everything",
        "ja": "最近なにかあった？ぼくに全部話して",
    },
    "c.missing.7": {
        "ko": "오늘은 꼭 네 얘기 듣고 싶어 요즘 어때?",
        "en": "I really want to hear from you today. How are things?",
        "ja": "今日はきみの話がすごく聞きたいな。最近どう？",
    },
    "c.long.1": {
        "ko": "요즘은 어떻게 지내? 오늘은 나랑 얘기하자",
        "en": "How have you been lately? Let's talk today",
        "ja": "最近どうしてる？今日はぼくと話そう",
    },
    "c.long.2": {
        "ko": "요즘 많이 바빴지? 잠깐 쉬면서 나랑 얘기할래?",
        "en": "Have you been busy lately? Want to take a quick break and talk with me?",
        "ja": "最近いそがしかったよね？ちょっと休んでぼくと話さない？",
    },
    "c.long.3": {
        "ko": "오늘 하루는 어땠어? 오랜만에 잠깐 얘기할까?",
        "en": "How was your day? Want to catch up for a bit?",
        "ja": "今日はどんな一日だった？ひさしぶりにちょっと話そうか？",
    },
    FALLBACK: {
        "ko": "오늘 얘기 들려줄래?",
        "en": "Will you tell me about your day?",
        "ja": "今日の話聞かせてくれる？",
    },
}

_CHAT_PREFIX = {
    MORE_CHAT: "c.more.",
    FIRST_TOUCH: "c.first.",
    DEFAULT_RECENT: "c.recent.",
    DEFAULT_MISSING: "c.missing.",
    DEFAULT_LONG: "c.long.",
}
_POOLS: dict[str, tuple[str, ...]] = {
    category: tuple(v for v in _CHAT if v.startswith(prefix)) for category, prefix in _CHAT_PREFIX.items()
}

_ROUTINE_BODY = {
    "ko": "오늘 루틴 미리 확인하고 하루를 시작해 볼까요?",
    "en": "How about checking today's routines before you start your day?",
    "ja": "今日のルーティンを確認して一日を始めませんか？",
}

# 루틴 아침 요일 제목 — isoweekday 순서(월=1 … 일=7).
WEEKDAYS: tuple[str, ...] = ("rm.mon", "rm.tue", "rm.wed", "rm.thu", "rm.fri", "rm.sat", "rm.sun")
ROUTINE_BREAK = "rm.break"
ROUTINE_RISK = "rr"

_ROUTINE: dict[str, dict[str, tuple[str, str]]] = {
    "rm.mon": {
        "ko": ("한 주의 시작, 월요일이에요 ❤️", _ROUTINE_BODY["ko"]),
        "en": ("It's Monday, a fresh start to the week ❤️", _ROUTINE_BODY["en"]),
        "ja": ("一週間のはじまり、月曜日です ❤️", _ROUTINE_BODY["ja"]),
    },
    "rm.tue": {
        "ko": ("화요일도 힘차게 시작해요 🧡", _ROUTINE_BODY["ko"]),
        "en": ("Let's start Tuesday strong 🧡", _ROUTINE_BODY["en"]),
        "ja": ("火曜日も元気にスタートしましょう 🧡", _ROUTINE_BODY["ja"]),
    },
    "rm.wed": {
        "ko": ("한 주의 절반, 수요일이에요 💛", _ROUTINE_BODY["ko"]),
        "en": ("It's Wednesday, halfway there 💛", _ROUTINE_BODY["en"]),
        "ja": ("一週間の折り返し、水曜日です 💛", _ROUTINE_BODY["ja"]),
    },
    "rm.thu": {
        "ko": ("주말까지 이틀 남았어요 💚", _ROUTINE_BODY["ko"]),
        "en": ("Just two days until the weekend 💚", _ROUTINE_BODY["en"]),
        "ja": ("週末まであと2日です 💚", _ROUTINE_BODY["ja"]),
    },
    "rm.fri": {
        "ko": ("드디어 금요일이에요 💙", _ROUTINE_BODY["ko"]),
        "en": ("It's finally Friday 💙", _ROUTINE_BODY["en"]),
        "ja": ("やっと金曜日です 💙", _ROUTINE_BODY["ja"]),
    },
    "rm.sat": {
        "ko": ("여유로운 토요일 아침이에요 💜", _ROUTINE_BODY["ko"]),
        "en": ("A relaxing Saturday morning 💜", _ROUTINE_BODY["en"]),
        "ja": ("ゆったりした土曜日の朝です 💜", _ROUTINE_BODY["ja"]),
    },
    "rm.sun": {
        "ko": ("한 주를 마무리하는 일요일이에요 🤍", _ROUTINE_BODY["ko"]),
        "en": ("It's Sunday, time to wrap up the week 🤍", _ROUTINE_BODY["en"]),
        "ja": ("一週間をしめくくる日曜日です 🤍", _ROUTINE_BODY["ja"]),
    },
    ROUTINE_BREAK: {
        "ko": ("아쉽게도 {lost}일 스트릭이 깨졌어요 😢", "오늘부터 다시 쌓아 볼까요? 💪"),
        "en": ("Sadly, your {lost} day streak has ended 😢", "Ready to build it back up starting today? 💪"),
        "ja": ("{lost}日連続の記録が途切れてしまいました 😢", "今日からまた積み重ねてみませんか？💪"),
    },
    ROUTINE_RISK: {
        "ko": ("오늘 안 하면 {n}일 스트릭이 깨져요 🚨", "지금 루틴 하나만 해도 유지할 수 있어요 🔥"),
        "en": ("Skip today and your {n} day streak breaks 🚨", "Just one routine now keeps it going 🔥"),
        "ja": ("今日やらないと{n}日連続の記録が途切れます 🚨", "今からルーティンをひとつやるだけで続けられます 🔥"),
    },
}

_DIARY = {
    "ko": "캐피의 새 일기가 도착했어요 📖",
    "en": "Cappy's new diary entry has arrived 📖",
    "ja": "キャピーの新しい日記が届きました 📖",
}


def pick(category: str) -> str:
    """대화 갈래 안에서 문구 id 하나를 무작위로."""
    return random.choice(_POOLS[category])


def chat_body(variant: str, language: str | None) -> str | None:
    """대화 문구 id의 본문. 대화 문구가 아니면 None(첫마디 이어 주기가 쓴다)."""
    table = _CHAT.get(variant)
    return i18n.pick(table, language) if table else None


def chat(variant: str, language: str | None) -> tuple[str, str]:
    return TITLE, i18n.pick(_CHAT[variant], language)


def routine(variant: str, language: str | None, *, n: int = 0, lost: int = 0) -> tuple[str, str]:
    title, body = i18n.pick(_ROUTINE[variant], language)
    return title.format(n=n, lost=lost), body


def diary(language: str | None) -> tuple[str, str]:
    return TITLE, i18n.pick(_DIARY, language)
