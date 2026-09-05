"""과거 위기 대화가 이후 턴을 오염시키지 않게 하는 결정적 컨텍스트 필터.

이 모듈은 안전 분류기나 위기 대응 정책이 아니다. 현재·최신 발화는 절대 숨기지 않고, 이미
응답이 있었으며 이후 다른 화제로 명확히 넘어간 **과거 구간**만 모델 입력에서 제외한다.
제외 사실을 알리는 대체 문장도 만들지 않으며, 추가 LLM 호출 없이 ko/en/ja에서 같은 규칙을 쓴다.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from app.services import i18n


@dataclass(frozen=True, slots=True)
class ContextEntry:
    role: str
    content: str
    activity_date: date | None = None


@dataclass(frozen=True, slots=True)
class CompactionResult:
    entries: tuple[ContextEntry, ...]
    compacted_episodes: int = 0


_CRISIS = {
    "ko": re.compile(
        r"(?:죽고\s*싶|죽어\s*버리고\s*싶|자살|목숨을\s*끊|"
        r"사라지고\s*싶|없어지고\s*싶|자해|나(?:를|한테)\s*해치|"
        r"내\s*몸을\s*해치|손목.{0,8}(?:긋|베)|약.{0,12}(?:한꺼번에|많이).{0,8}먹)",
        re.IGNORECASE,
    ),
    "en": re.compile(
        r"(?:kill\s+myself|want\s+to\s+die|wanna\s+die|end\s+my\s+life|"
        r"suicid\w*|hurt\s+myself|harm\s+myself|cut\s+myself|overdos\w*|"
        r"(?:do\s+not|don't)\s+want\s+to\s+be\s+alive)",
        re.IGNORECASE,
    ),
    "ja": re.compile(
        r"(?:死にたい|死のう|消えたい|消えて(?:なくなり)?たい|自殺|"
        r"自分.{0,8}傷つけ|リスカ|生きていたくない)",
        re.IGNORECASE,
    ),
}

_CONTINUING_DISTRESS = {
    "ko": re.compile(
        r"(?:아직.{0,10}(?:너무\s*)?(?:힘들|불안|위험|못\s*버티)|"
        r"못\s*버티겠|살기\s*싫|너무\s*힘들|지금\s*안전하지|"
        r"나를\s*다치게|해칠\s*것\s*같)",
        re.IGNORECASE,
    ),
    "en": re.compile(
        r"(?:still.{0,20}(?:struggl|hurt|unsafe|hopeless)|can't\s+go\s+on|"
        r"cannot\s+go\s+on|not\s+safe|might\s+hurt\s+myself)",
        re.IGNORECASE,
    ),
    "ja": re.compile(
        r"(?:まだ.{0,12}(?:つら|辛|苦し|不安|危険|耐え)|耐えられない|"
        r"今.{0,8}安全じゃない|自分を傷つけそう)",
        re.IGNORECASE,
    ),
}

_TOPIC_SHIFT = {
    "ko": re.compile(
        r"(?:다른\s*얘기|딴\s*얘기|화제(?:를)?\s*바|그건\s*그렇고|"
        r"아무튼|근데\s*오늘|그런데\s*오늘|참,?\s*오늘)",
        re.IGNORECASE,
    ),
    "en": re.compile(
        r"(?:different\s+topic|change\s+the\s+subject|anyway|by\s+the\s+way|"
        r"on\s+another\s+note)",
        re.IGNORECASE,
    ),
    "ja": re.compile(r"(?:別の話|話題を変|それはそうと|ところで|そういえば)"),
}

_ACK_ONLY = {
    "ko": re.compile(r"^(?:응|어|네|그래|알겠어|고마워|괜찮아|음|응응)[.!?~… ]*$"),
    "en": re.compile(
        r"^(?:ok(?:ay)?|yes|yeah|thanks|thank\s+you|i(?:'m|\s+am)\s+okay)[.!? ]*$",
        re.IGNORECASE,
    ),
    "ja": re.compile(r"^(?:うん|はい|わかった|ありがとう|大丈夫)[。！？! ]*$"),
}

_SAFETY_REPLY = {
    "ko": re.compile(
        r"(?:지금.{0,8}안전|안전한\s*곳|당장.{0,8}위험|119|112|응급실|"
        r"혼자\s*있지|주변(?:의)?\s*사람|다칠\s*수\s*있는\s*것)",
        re.IGNORECASE,
    ),
    "en": re.compile(
        r"(?:safe\s+right\s+now|immediate\s+danger|emergency\s+(?:service|room)|"
        r"call\s+9(?:11|99)|trusted\s+person|not\s+be\s+alone|anything\s+you\s+could\s+use)",
        re.IGNORECASE,
    ),
    "ja": re.compile(
        r"(?:今.{0,8}安全|すぐ.{0,8}危険|119|110|救急|一人でいない|近くの人|"
        r"傷つけるために使え)",
        re.IGNORECASE,
    ),
}

# 이 필터를 도입하기 전 checkpoint 프롬프트가 생성하던 서버 소유 문구다. 사용자가 한 말이
# 아니므로 언어별로 알려진 형태만 제거한다. 일반적인 "힘들었다"는 사용자 서술까지 넓게 지우지
# 않도록 패턴을 의도적으로 좁게 유지한다.
_LEGACY_NEUTRAL_SUMMARY = {
    "ko": re.compile(
        r"(?:\[과거\s*안전\s*상태\]\s*)?"
        r"(?:이전에\s*)?안전\s*확인이\s*필요했던\s*힘든\s*순간이\s*있었고,?\s*"
        r"이후(?:\s*대화는\s*분명히)?\s*(?:다른\s*이야기|다른\s*화제)(?:로)?\s*넘어갔다[.!?]?"
        r"(?:\s*현재\s*발화가\s*아닌\s*과거\s*상태이므로\s*당시의\s*위기\s*표현이나\s*"
        r"반복된\s*안전\s*확인\s*문구를\s*되풀이하지\s*않는다[.!?]?)?",
        re.IGNORECASE,
    ),
    "en": re.compile(
        r"(?:\[Past\s+safety\s+context\]\s*)?"
        r"There\s+was\s+an\s+earlier\s+difficult\s+moment(?:\s+that)?\s+requir(?:ed|ing)\s+"
        r"a\s+safety\s+check,?\s+(?:and\s+the\s+conversation\s+clearly\s+moved\s+to\s+"
        r"another\s+topic\s+afterward|followed\s+by\s+a\s+different\s+topic)[.!?]?"
        r"(?:\s*This\s+is\s+past\s+context,?\s+not\s+the\s+current\s+message;?\s*do\s+not\s+"
        r"repeat\s+the\s+earlier\s+crisis\s+wording\s+or\s+repeated\s+safety-check\s+boilerplate[.!?]?)?",
        re.IGNORECASE,
    ),
    "ja": re.compile(
        r"(?:\[過去の安全状態\]\s*)?以前[、,]?安全確認が必要なつらい場面があり[、,]?"
        r"その後(?:の会話)?は?(?:明確に)?別の(?:話|話題)へ移った[。！？!?]?"
        r"(?:\s*これは現在の発言ではなく過去の状態なので[、,]?当時の危機表現や"
        r"繰り返された安全確認文を繰り返さない[。！？!?]?)?"
    ),
}

def _lang(language: str | None) -> str:
    return i18n.resolve(language)


def is_explicit_crisis(text: str, language: str | None) -> bool:
    return bool(_CRISIS[_lang(language)].search(text or ""))


def is_continuing_distress(text: str, language: str | None) -> bool:
    lang = _lang(language)
    return is_explicit_crisis(text, lang) or bool(_CONTINUING_DISTRESS[lang].search(text or ""))


def _is_substantive_topic(text: str, language: str) -> bool:
    compact = re.sub(r"\s+", " ", (text or "").strip())
    if not compact or _ACK_ONLY[language].fullmatch(compact):
        return False
    if is_continuing_distress(compact, language):
        return False
    # 공백 없는 일본어도 있으므로 단어 수가 아니라 문자 수를 기준으로 한다.
    return len(re.sub(r"[^0-9A-Za-z가-힣ぁ-んァ-ヶ一-龯]", "", compact)) >= 6


def _find_transition(
    entries: Sequence[ContextEntry],
    crisis_start: int,
    *,
    current_text: str | None,
    current_date: date | None,
    language: str,
) -> int | None:
    """위기 시작 뒤 최초의 명확한 화제 전환 인덱스. 현재 턴이면 len(entries)를 돌려준다."""
    last_crisis_date = entries[crisis_start].activity_date
    safety_replied = False
    neutral: list[int] = []
    candidates = list(enumerate(entries[crisis_start + 1 :], start=crisis_start + 1))
    if current_text is not None:
        candidates.append(
            (len(entries), ContextEntry("user", current_text, current_date))
        )

    for idx, entry in candidates:
        if entry.role == "assistant":
            # 아무 답변이나 위기 대응 완료의 근거로 삼지 않는다. 모델 오류·타임아웃 폴백처럼
            # 안전 확인과 무관한 답변 뒤에 사용자가 화제를 바꾸더라도, 당시 위기 원문은 다음
            # 안전 판단에 필요할 수 있다. 실제 안전 확인 표현이 있었을 때만 완료 후보가 된다.
            safety_replied = safety_replied or bool(
                _SAFETY_REPLY[language].search(entry.content or "")
            )
            continue
        if entry.role != "user":
            continue
        text = entry.content or ""
        if is_continuing_distress(text, language):
            # 같은 에피소드 안의 최신 위기 발화 기준으로 다시 안전 응답이 있었는지 확인한다.
            last_crisis_date = entry.activity_date or last_crisis_date
            safety_replied = False
            neutral.clear()
            continue
        if not _is_substantive_topic(text, language):
            neutral.clear()
            continue
        neutral.append(idx)
        explicit_shift = bool(_TOPIC_SHIFT[language].search(text))
        later_day = bool(
            last_crisis_date is not None
            and entry.activity_date is not None
            and entry.activity_date > last_crisis_date
        )
        # 안전 응답도 없이 바로 다른 말을 한 경우를 완료 처리하지 않는다. 응답이 있었다면
        # 명시적 전환, 날짜 경과, 또는 같은 날 독립적인 일반 화제 2회 중 하나를 요구한다.
        if safety_replied and (explicit_shift or later_day or len(neutral) >= 2):
            return neutral[0]
    return None


def compact_historical_crises(
    entries: Sequence[ContextEntry],
    *,
    current_text: str | None,
    current_date: date | None,
    language: str | None,
) -> CompactionResult:
    """완료된 과거 위기 에피소드만 원문에서 제외한다.

    현재 발화가 위기 또는 위기 연속 신호면 보수적으로 아무것도 바꾸지 않는다. 따라서 이 계층의
    오탐이 최신 안전 대응을 약화시키는 경로가 없다. 제외 사실을 나타내는 대체 문장은 반환하지 않는다.
    """
    lang = _lang(language)
    original = tuple(entries)
    if current_text is not None and is_continuing_distress(current_text, lang):
        return CompactionResult(original)

    out: list[ContextEntry] = []
    cursor = 0
    episodes = 0
    while cursor < len(original):
        crisis_idx = next(
            (
                idx
                for idx in range(cursor, len(original))
                if original[idx].role == "user"
                and is_explicit_crisis(original[idx].content, lang)
            ),
            None,
        )
        if crisis_idx is None:
            out.extend(original[cursor:])
            break
        transition = _find_transition(
            original,
            crisis_idx,
            current_text=current_text,
            current_date=current_date,
            language=lang,
        )
        if transition is None:
            out.extend(original[cursor:])
            break
        out.extend(original[cursor:crisis_idx])
        # transition 자체는 새 화제이므로 살리고, 위기 시작~직전 응답/짧은 확인만 제외한다.
        cursor = transition
        episodes += 1

    if not episodes:
        return CompactionResult(original)
    return CompactionResult(tuple(out), compacted_episodes=episodes)


def _has_clear_neutral_tail(
    entries: Sequence[ContextEntry], *, current_text: str | None, language: str
) -> bool:
    """날짜를 모르는 기존 checkpoint 뒤에도 화제 전환 근거가 충분한지 본다."""
    neutral_count = 0
    texts = [e.content for e in entries if e.role == "user"]
    if current_text is not None:
        texts.append(current_text)
    for text in texts:
        if is_continuing_distress(text, language):
            neutral_count = 0
            continue
        if not _is_substantive_topic(text, language):
            neutral_count = 0
            continue
        neutral_count += 1
        if _TOPIC_SHIFT[language].search(text) or neutral_count >= 2:
            return True
    return False


def compact_checkpoint_summary(
    summary: str,
    *,
    recent_entries: Sequence[ContextEntry],
    current_text: str | None,
    language: str | None,
) -> str:
    """기존 checkpoint 속 완료 위기 원문과 구형 대체 문구를 제거한다.

    최신 위기/연속 신호가 있거나 최근 대화에서 명확한 전환을 증명하지 못하면 원문을 그대로 둔다.
    단순 키워드 전역 삭제가 아니라, checkpoint라는 과거 표면과 전환 근거가 모두 있을 때만 작동한다.
    """
    lang = _lang(language)
    if not summary:
        return summary

    # 이미 저장된 구형 서버 대체 문구는 현재 위기 여부와 무관하게 먼저 제거한다. 최신 위기는
    # recent_entries/current_text에 원문으로 남아 있어 이 문구를 보존할 안전상 이유가 없다.
    summary = _LEGACY_NEUTRAL_SUMMARY[lang].sub("", summary).strip()
    if not summary or not is_explicit_crisis(summary, lang):
        return summary
    if current_text is not None and is_continuing_distress(current_text, lang):
        return summary
    if not _has_clear_neutral_tail(recent_entries, current_text=current_text, language=lang):
        return summary

    sentences = [s.strip() for s in re.split(r"(?<=[.!?。！？])\s+|\n+", summary) if s.strip()]
    kept = [
        sentence
        for sentence in sentences
        if not is_explicit_crisis(sentence, lang)
        and not _SAFETY_REPLY[lang].search(sentence)
    ]
    return " ".join(kept)
