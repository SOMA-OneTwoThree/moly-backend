"""오늘의 작은 실천 원고 — 프롬프트, 기계 검증, 중복 제거. 시드 스크립트와 보충 잡이 함께 쓴다.

프롬프트에는 카테고리와 시간대, 기존 제목만 들어간다. 사용자 정보는 어떤 경로로도 넣지 않는다.
"""
from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import settings
from app.services import llm, usage_ledger
from app.services.daily_practice_selection import CATEGORIES  # noqa: F401 — 잡·스크립트가 이 모듈에서 읽는다

PROMPT_VERSION = "daily-practice-v1"
SLOTS = ("morning", "day", "evening", "night")
LANGUAGES = ("ko", "en", "ja")
TITLE_MAX = {"ko": 18, "en": 32, "ja": 18}
SPEECH_MAX = {"ko": 40, "en": 100, "ja": 50}
_ICONS_PATH = Path(__file__).resolve().parents[1] / "resources/daily_practice/icons.json"
_PROMPT_AVOID_MAX = 150
_NEAR_DUPLICATE = 0.5

CATEGORY_BRIEF = {
    "rest": "sleep and winding down: getting ready for bed, resting the eyes, lying down for a moment",
    "body": "moving and waking the body up gently: stretching, standing up, a short walk indoors or outside",
    "calm": "easing stress and worry right now: slow breathing, relaxing the shoulders, stepping away from a screen",
    "mind": "sorting out thoughts: writing one worry or one to-do on paper, noting what is on your mind",
    "self_kindness": "being kind to yourself: writing one thing you did fine today, stopping one thing that can wait",
    "connect": (
        "reaching out to someone. It must work for a person with no close friends: a short thank-you to anyone, "
        "or replying to one message they had put off. Never assume a friend or family member exists."
    ),
    "order": "tiny acts of keeping the day or the space in order: clearing one surface, deciding tomorrow's first task",
}

_BANNED = {
    "ko": re.compile(
        r"다이어트|체중|몸무게|칼로리|살 ?빼|단식|금식|음주|맥주|소주|와인|(?<![가-힣])술|담배|흡연|금연|커피|카페인"
        r"|(?<![가-힣])약(?!속)|복용|치료|상담|우울|불안장애|증상|회사|직장|상사|동료|출근|퇴근|학교|선생님|수업"
        r"|남자친구|여자친구|애인|남편|아내|배우자|부모님|엄마|아빠|자동차|(?<![가-힣])차를|운전"
        r"|강아지|고양이|반려|기도|교회|성당|불교|결제|구매|(?<![가-힣])돈|전력|힐링|치유|온전히|오롯이|내면"
        r"|소중|토닥|포근|위로|여정|마음챙김|마음"
    ),
    "en": re.compile(
        r"\b(?:diet|weight|calorie|alcohol|beer|wine|smok|cigarette|coffee|caffeine|medicine|medication|pill|therap"
        r"|heal|cure|depress|partner|spouse|husband|wife|boyfriend|girlfriend|parent|mom|dad|mother|father|boss"
        r"|cowork|colleague|office|job|work|school|teacher|class|car|drive|dog|cat|pet|pray|church|temple|buy"
        r"|purchase|money|sprint|workout|journey|soul|cozy|cherish|gentle)\w*\b",
        re.IGNORECASE,
    ),
    "ja": re.compile(
        r"ダイエット|体重|カロリー|お酒|ビール|ワイン|タバコ|たばこ|禁煙|コーヒー|カフェイン|薬|治療|療法|カウンセ"
        r"|会社|職場|上司|同僚|学校|先生|恋人|彼氏|彼女|(?<!大丈)夫|妻|両親|親に|親へ|母|父|車|運転|犬|猫|ペット"
        r"|祈|教会|神社|買|購入|お金|全力|癒|心|自分を大切"
    ),
}
_STIFF = {
    "ko": re.compile(r"(?:요|니다|세요|시오)(?:[.?\s]|$)"),
    "ja": re.compile(r"です|ます|ましょう|ください"),
}
_FORBIDDEN_MARKS = re.compile(r"[·・•!！]")
_SENTENCE_END = re.compile(r"[.?。？?]+(?:\s|$)")
_SCRIPT = {
    "ko": re.compile(r"^[가-힣0-9 ,.?…]+$"),
    "en": re.compile(r"^[A-Za-z0-9 ,.?'’\-]+$"),
    "ja": re.compile(r"^[぀-ヿ一-鿿0-9０-９ー、。?？…〜]+$"),
}
# 특정 시간대에만 맞는 말. 왼쪽 표현을 쓰면 slots가 오른쪽 부분집합이어야 한다.
_TIME_WORDS = (
    (re.compile(r"아침|morning|朝", re.IGNORECASE), {"morning"}),
    (re.compile(r"햇빛|햇살|sunlight|sunshine|日差し|日光", re.IGNORECASE), {"morning", "day"}),
    (re.compile(r"잠들|자기 전|잠자리|bedtime|before bed|before sleep|寝る前|布団", re.IGNORECASE), {"evening", "night"}),
    (re.compile(r"오늘 밤|tonight|今夜", re.IGNORECASE), {"evening", "night"}),
    (re.compile(r"저녁|evening|夕方|夜", re.IGNORECASE), {"evening", "night"}),
)


@lru_cache
def allowed_icons() -> tuple[str, ...]:
    return tuple(json.loads(_ICONS_PATH.read_text(encoding="utf-8")))


@dataclass(frozen=True, slots=True)
class Action:
    category: str
    slots: tuple[str, ...]
    icon: str
    title: dict[str, str]
    speech: dict[str, str]


@dataclass(slots=True)
class Batch:
    accepted: list[Action] = field(default_factory=list)
    rejected: list[tuple[str, list[str]]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


def system_prompt(category: str, slot: str | None) -> str:
    slot_rule = (
        f"- At least half of the actions must fit the {slot} slot.\n" if slot is not None else ""
    )
    return f"""You write "today's small practice" offers for a mobile app. Cappy, a capybara who lives alone in a city home, offers the user one small action a day. The user can check it off, and later keep it as a daily routine. Write one category: {category} ({CATEGORY_BRIEF[category]}).

What makes a good action:
- A small thing anyone can do alone in about five minutes, today, at no cost, with no special equipment.
- Checkable: afterwards you can tell whether you did it. "Write one to-do on paper" is checkable. "Relax" is not.
- Ordinary. It should sound like something a person would really do on a normal day.
- Vary the actions. Do not write the same idea twice with different words.

Never include:
- medical or therapeutic claims, medication, diet, weight, calories, alcohol, tobacco, caffeine as a fix, intense exercise, anything risky, spending money
- anything that assumes a partner, parents, a job, a school, a car, a pet or a religion
- anything that induces guilt, extreme or heavy situations
- metaphors, sentimental decoration, inner-monologue phrasing, translationese

Plain everyday words only. Each language is written natively, not translated word for word.
Never use a middle dot (· or ・). Never use an exclamation mark. No emoji.

title: reads as a routine name, a plain short phrase.
- ko: at most 18 characters, ends in a noun form with 기, like "물 한 컵 마시기" or "창문 열어 두기".
- en: at most 32 characters, starts with a capital, imperative, no period, like "Drink a glass of water".
- ja: at most 18 characters, ends in a dictionary form or noun, like "お水を一杯飲む".

speech: what Cappy says when offering it. One or two short sentences. Spoken casually to a friend, with no fuss.
- ko: at most 40 characters, 반말 only. Plain suggestions like "물 한 컵 마시고 올래?", "오늘은 창문 좀 열어 볼까?", "귀찮으면 한 번만 해 봐도 돼."
- en: at most 100 characters, relaxed and plain. Like "How about a glass of water?", "Want to open the window for a bit?"
- ja: at most 50 characters, タメ口 only, never です or ます. Cappy calls himself ぼく if needed. Like 「お水、一杯飲んでこない?」「ちょっと窓を開けてみる?」
Cappy does not cheer, praise, explain benefits, or promise results. He does not claim to have done anything himself.

slots: every time of day where the action makes sense, using only morning (05-11), day (11-17), evening (17-22), night (22-05). Use several whenever it fits. Something like opening the curtains to the sun is only morning or day; getting ready for bed is only evening or night.
{slot_rule}
icon: the one key from this list that best matches the action, copied exactly: {", ".join(allowed_icons())}

Reply with one JSON object only, no markdown:
{{"actions": [{{"slots": ["morning"], "icon": "droplet", "title": {{"ko": "...", "en": "...", "ja": "..."}}, "speech": {{"ko": "...", "en": "...", "ja": "..."}}}}]}}"""


def user_prompt(count: int, avoid: Sequence[str]) -> str:
    listed = "\n".join(f"- {title}" for title in avoid[:_PROMPT_AVOID_MAX])
    if not listed:
        return f"Write {count} actions."
    return f"Write {count} actions. These already exist, so write different ideas:\n{listed}"


def parse(text: str) -> list[Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return []
    actions = data.get("actions") if isinstance(data, dict) else None
    return actions if isinstance(actions, list) else []


def _sentences(text: str) -> int:
    return len(_SENTENCE_END.findall(text)) or 1


def problems(raw: Any) -> list[str]:
    """쓰기 규칙 중 기계로 확인할 수 있는 것. 비어 있으면 통과다."""
    if not isinstance(raw, dict):
        return ["not_an_object"]
    found: list[str] = []
    slots = raw.get("slots")
    if (not isinstance(slots, list) or not slots
            or any(s not in SLOTS for s in slots) or len(set(slots)) != len(slots)):
        found.append("slots")
        slots = []
    if raw.get("icon") not in allowed_icons():
        found.append("icon")
    for name, limits in (("title", TITLE_MAX), ("speech", SPEECH_MAX)):
        texts = raw.get(name)
        if not isinstance(texts, dict) or any(not isinstance(texts.get(k), str) for k in LANGUAGES):
            found.append(name)
            continue
        for language in LANGUAGES:
            value = unicodedata.normalize("NFC", texts[language])
            label = f"{name}_{language}"
            if not value.strip() or value != value.strip() or len(value) > limits[language]:
                found.append(f"{label}_length")
            if _FORBIDDEN_MARKS.search(value):
                found.append(f"{label}_mark")
            if not _SCRIPT[language].match(value):
                found.append(f"{label}_script")
            if _BANNED[language].search(value):
                found.append(f"{label}_banned")
            pattern = _STIFF.get(language)
            if name == "speech" and pattern is not None and pattern.search(value):
                found.append(f"{label}_formal")
            if name == "speech" and _sentences(value) > 2:
                found.append(f"{label}_sentences")
            if name == "title" and re.search(r"[.?,。？?、]", value):
                found.append(f"{label}_punctuation")
            for words, allowed in _TIME_WORDS:
                if words.search(value) and not set(slots) <= allowed:
                    found.append(f"{label}_time_word")
    title = raw.get("title")
    if isinstance(title, dict) and isinstance(title.get("ko"), str) and not title["ko"].endswith("기"):
        found.append("title_ko_ending")
    if isinstance(title, dict) and isinstance(title.get("en"), str) and not title["en"][:1].isupper():
        found.append("title_en_capital")
    return found


def _bare(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFC", text).casefold() if c.isalnum())


def _bigrams(text: str) -> set[str]:
    bare = _bare(text)
    return {bare[i:i + 2] for i in range(len(bare) - 1)} or {bare}


def is_duplicate(title: str, existing: Iterable[str]) -> bool:
    bare = _bare(title)
    grams = _bigrams(title)
    for other in existing:
        if bare == _bare(other):
            return True
        other_grams = _bigrams(other)
        if len(grams & other_grams) / len(grams | other_grams) >= _NEAR_DUPLICATE:
            return True
    return False


def to_action(raw: dict[str, Any], category: str) -> Action:
    return Action(
        category=category,
        slots=tuple(s for s in SLOTS if s in raw["slots"]),
        icon=raw["icon"],
        title={k: unicodedata.normalize("NFC", raw["title"][k]) for k in LANGUAGES},
        speech={k: unicodedata.normalize("NFC", raw["speech"][k]) for k in LANGUAGES},
    )


def _title_for_log(raw: Any) -> str:
    title = raw.get("title") if isinstance(raw, dict) else None
    return str(title.get("ko", "")) if isinstance(title, dict) else ""


async def generate(
    category: str,
    count: int,
    *,
    existing_titles: Iterable[str],
    avoid_in_prompt: Sequence[str] = (),
    slot: str | None = None,
    purpose: str,
    job_id: uuid.UUID | None = None,
    max_tokens: int,
    timeout: float,
) -> Batch:
    """한 번의 LLM 호출로 count개를 받아 검증과 중복 제거를 통과한 것만 돌려준다.

    짧은 JSON 판정이 아니라 원고 품질이 중요해 추론을 낮게 켠다. 추론 토큰도 max_tokens에서 빠지므로
    호출측은 답 길이보다 넉넉한 상한을 준다.
    """
    result = await llm.generate(
        system_prompt(category, slot),
        [{"role": "user", "content": user_prompt(count, avoid_in_prompt)}],
        model=settings.model_daily_practice,
        max_tokens=max_tokens,
        reasoning_effort="low",
        timeout=timeout,
        ledger=usage_ledger.LedgerContext(
            lane=usage_ledger.LANE_BACKGROUND, purpose=purpose, job_id=job_id,
            prompt_version=PROMPT_VERSION,
        ),
    )
    batch = Batch(input_tokens=result.input_tokens, output_tokens=result.output_tokens)
    seen = list(existing_titles)
    for raw in parse(result.text):
        found = problems(raw)
        title = raw["title"]["ko"] if not found else _title_for_log(raw)
        if not found and is_duplicate(title, seen):
            found = ["duplicate"]
        if found:
            batch.rejected.append((title, found))
            continue
        seen.append(title)
        batch.accepted.append(to_action(raw, category))
    return batch

