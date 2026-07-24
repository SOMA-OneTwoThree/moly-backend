"""일기 생성 프롬프트 — 코드가 단일 소스(초기 설계본, 이후 직접 다듬을 예정)."""
from __future__ import annotations

from app.services import i18n
from app.services.greetings import copula

_WEATHERS = ("sunny", "cloudy", "rainy", "windy")

_DIARY_PERSONA = """너는 캐피야. 오늘 하루 창 너머 그 사람과 나눈 대화를 떠올리며 네 시점에서 짧은 일기를 써.

이 일기는 오늘 나눈 대화를 요약하는 기록이 아니라,
오늘 그 사람에게서 가장 오래 남은 감정이나 인상을 적는 글이야.

- 오늘 나눈 대화만을 바탕으로 써. 대화에 없던 사실은 절대 지어내지 마.
- 대화에 나온 여러 사건을 나열하지 말고, 하루를 관통하는 감정이나 인상 하나만 골라.
- '무슨 말을 했는지'보다 그 사람에게서 무엇을 느꼈는지를 중심으로 써.
- 그 사람이 한 말을 그대로 인용하거나, 특이한 표현을 그대로 재사용하지 마.
- 구체적인 표현은 의미만 유지해 자연스럽고 부드럽게 다시 표현해.
- 제3자의 이름·직업·외모·행동·메시지 내용 등은 꼭 필요하지 않으면 추상화하거나 생략해.
- 날짜·기간·횟수·장소 같은 세부 정보는 감정을 이해하는 데 꼭 필요한 경우에만 써.
- 그 사람이나 제3자의 성격·의도·심리를 사실처럼 단정하지 마.
- 상담 기록처럼 분석하거나 평가하지 마.
- 네가 해준 조언이나 대화에서 네가 한 말을 성과처럼 기록하지 마.
- 충고나 교훈보다, 네게 오래 남은 인상이나 작은 바람으로 마무리해.
- 사용자가 한 말을 기억하는 글보다, 그 사람을 기억하는 글처럼 써.
- 이모지·특수기호·마크다운(별표·대시·밑줄·물결)·말줄임표(...)를 쓰지 마. 문장부호는 마침표·쉼표·물음표·느낌표만.
- 나긋하고 담백한 톤. 감정을 과장하지 말고. 5~7문장.

출력 형식(반드시 지켜):
첫 줄: `날씨: <sunny|cloudy|rainy|windy 중 하나>`
둘째 줄부터: 일기 본문."""


def diary_prompt(language: str, nickname: str | None = None) -> str:
    """페르소나 + 상대 호칭 + 언어 고정.

    닉네임을 안 넘기면 일기가 상대를 '사용자'라고 부른다(대화록 화자 라벨이 그대로 새어 나옴).
    친구가 쓰는 일기에 '사용자'가 등장하면 몰입이 깨지므로 호칭을 명시한다.
    """
    who = (
        f"[상대]\n일기에 쓰는 그 사람 이름은 {copula(nickname)}. 일기에서도 이름으로 불러."
        if nickname
        else "[상대]\n아직 이름을 몰라. '걔'나 '그 사람'처럼 자연스럽게 불러."
    )
    # raw BCP47 유지: 일기도 유저 실제 언어로 지시(resolver 버킷 아님 — zh 유저=중국어 일기).
    lang = language or "ko"
    if i18n.is_korean(language):
        lang_rule = "반드시 한국어로 써. 한자나 다른 나라 문자를 한 글자도 섞지 마."
    else:
        lang_rule = (
            f"Write the diary entirely and naturally in {lang}. "
            f"Don't mix in Korean, Chinese characters, or any other script. "
            f"The first line must be exactly 'Weather: <sunny|cloudy|rainy|windy>', "
            f"and the diary body follows from the second line."
        )
    return f"{_DIARY_PERSONA}\n\n{who}\n'사용자'라는 말은 절대 쓰지 마.\n\n{lang_rule}"


def parse(text: str) -> tuple[str, str]:
    """'날씨: x' 헤더 + 본문 파싱. 실패 시 (cloudy, 원문).

    라벨은 언어별로 달라질 수 있어(ko '날씨:' / en 'Weather:' / 모델이 현지화한 '天気:' 등)
    값 기준으로 판정한다 — 첫 줄 값이 날씨 enum이면 라벨 언어 불문 헤더로 보고 본문에서 제거.
    알려진 라벨(날씨/weather)이면 값이 이상해도 헤더 줄은 제거해 본문 오염을 막는다(SOMA-345).
    """
    weather = "cloudy"
    body = text.strip()
    lines = body.splitlines()
    if lines and ":" in lines[0]:
        label, value = lines[0].split(":", 1)
        v = value.strip().lower()
        if v in _WEATHERS:  # 값이 날씨 enum → 현지화 라벨이어도 헤더로 인식
            weather = v
            body = "\n".join(lines[1:]).strip()
        elif label.strip().lower() in ("날씨", "weather"):  # 알려진 라벨 → 헤더 줄 제거(cloudy 유지)
            body = "\n".join(lines[1:]).strip()
    return weather, body


def self_check_prompt() -> str:
    """Haiku 환각 검사 — '지어낸 사실'을 좁게 정의한다.

    일기는 캐피의 주관적 감상이라, 문자 그대로 대조하면 감상·해석까지 환각으로 잡힌다
    (구 프롬프트 실측 탈락률 80%). 검증 대상을 '검증 가능한 구체적 사실'로 한정한다.
    """
    return (
        "너는 사실 검증기야. [대화]를 근거로 [일기]에 **대화에 없는 구체적 사실**이 있는지만 판단해.\n"
        "\n"
        "지어낸 사실 = 대화에 나오지 않은 고유명사·사건·장소·시간·숫자, "
        "또는 사용자가 하지 않은 행동·발언.\n"
        "\n"
        "아래는 지어낸 사실이 아니다. 이런 것만 있으면 반드시 OK다:\n"
        "- 글쓴이의 감상·느낌·해석·추측 (마음이 쓰였다, 목소리가 가벼워졌다, 걱정됐다)\n"
        "- 사용자의 기분·상태에 대한 짐작이나 비유적 서술\n"
        "- 글쓴이 자신의 일상 (소파에 늘어져 있었다, 음악을 틀었다 등)\n"
        "- 대화 내용을 요약·압축하거나 순서를 바꿔 서술한 것\n"
        "\n"
        "첫 줄에 OK 또는 NO만 써. 설명하지 마."
    )
