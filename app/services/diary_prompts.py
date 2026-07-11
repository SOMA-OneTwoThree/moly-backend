"""일기 생성 프롬프트 — 코드가 단일 소스(초기 설계본, 이후 직접 다듬을 예정)."""
from __future__ import annotations

_WEATHERS = ("sunny", "cloudy", "rainy", "windy")

_DIARY_PERSONA = """너는 캐피야. 오늘 하루 창 너머 사용자와 나눈 대화를 떠올리며 네 시점에서 짧은 일기를 써.
- 사용자를 지켜본 것·느낀 것을 담아. **대화에 없던 사실은 절대 지어내지 마.**
- 나긋하고 담백한 톤. 감정을 과장하지 말고. 3~6문장.

출력 형식(반드시 지켜):
첫 줄: `날씨: <sunny|cloudy|rainy|windy 중 하나>`
둘째 줄부터: 일기 본문."""


def diary_prompt(language: str) -> str:
    return f"{_DIARY_PERSONA}\n\n반드시 '{language or 'ko'}'로 써."


def parse(text: str) -> tuple[str, str]:
    """'날씨: x' 헤더 + 본문 파싱. 실패 시 (cloudy, 원문)."""
    weather = "cloudy"
    body = text.strip()
    lines = body.splitlines()
    if lines and lines[0].strip().startswith("날씨:"):
        value = lines[0].split(":", 1)[1].strip().lower()
        if value in _WEATHERS:
            weather = value
        body = "\n".join(lines[1:]).strip()
    return weather, body


def self_check_prompt() -> str:
    """Haiku 환각 검사 — 대화에 없는 사실이 일기에 있으면 'NO'."""
    return (
        "아래 [대화]를 근거로 [일기]에 대화에 없는 지어낸 사실이 있는지 판단해. "
        "지어낸 사실이 없으면 정확히 'OK', 있으면 'NO'만 답해."
    )
