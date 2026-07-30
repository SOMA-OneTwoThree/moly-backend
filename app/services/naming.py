"""닉네임 마스킹 — 저장 시 이름 '스템'만 `{유저이름}` 토큰으로 마스킹, egress에서 현재 이름으로 치환.

개명 시 과거 대화·기억·클라 이력이 자동으로 현재 이름으로 보인다.

설계(2026-07-20 확정):
- 생성(LLM)엔 현재 실제 닉네임을 준다 → 캐피가 조사까지 자연스럽게 부른다("승민아", "승민이가").
- 저장 직전 `to_placeholder`가 **이름 스템만** `{유저이름}`으로 바꾼다. 조사(아/야·이가·씨·님·의…)는
  리터럴로 남으므로 조사 형태를 열거할 필요가 없다(구 방식의 '이가' 누락 버그 소멸).
- egress·LLM 재투입 `render`가 `{유저이름}`을 현재 이름으로 치환한다. 받침 의존 조사(아/야·이/가·
  이가·은/는·을/를·과/와·(이)라고·(이)랑·(이)나)가 **파티클로 붙어 있으면**(뒤가 비한글 경계) 현재
  이름 받침에 맞게 재계산한다. 뒤에 한글이 이어지면(예: '승민아파트') 단어로 보고 리터럴 유지.

불변식: 저장 표면(messages/greetings/diaries.content)에 실제 이름 스템이 남지 않는다.
(mem0·chat_contexts.memory_text는 별도 best-effort — mem0 custom_instructions가 이름 미저장.)
"""
from __future__ import annotations

import re
import unicodedata

from app.services import greetings

TOKEN = "{유저이름}"
_TOK_RE = re.escape(TOKEN)

# 라틴계 글자·숫자 — 단어 경계 판정용. 한글은 조사가 붙으므로 제외.
# À-ɏ=U+00C0–024F(악센트·라틴 확장 A/B), Ḁ-ỿ=U+1E00–1EFF(베트남 등 확장 추가, Tuệ의 ệ=U+1EC7 등). SOMA-365.
# ⚠️ CJK(한자·가나)는 띄어쓰기가 없어 naive substring 마스킹이 과치환(愛⊂恋愛)한다. 그래서 조건부로만
#   마스킹한다(2글자+ & 뒤가 안전 조사·경칭·부호·경계) — _CJK_NAME_TAIL·to_placeholder 참조(SOMA-365).
#   비조사 연속(まお元気)은 미마스킹 잔여이나 기밀 방어선은 RLS+mem0 이름미저장이 담당.
_LATIN = r"A-Za-z0-9À-ɏḀ-ỿ"
_LATIN_RE = re.compile(rf"[{_LATIN}]")
# CJK(가나·한자·CJK 통합/확장A/호환) 판정 — 이 스크립트를 포함하는 닉네임은 조건부 마스킹(위 주석·SOMA-365).
_CJK_RE = re.compile("[\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9d\U00020000-\U0002fa1f]")

# CJK 이름 뒤 경계(SOMA-365 후속) — 일본어는 띄어쓰기가 없어 naive substring이 과치환하므로
# (愛⊂恋愛·さくら⊂さくらんぼ), 뒤가 문장경계·문장부호·안전한 일본어 조사(助詞)·경칭일 때만 마스킹.
# 뒤가 일반 가나·한자 연속이면(さくら+'ん'·健太+'郎') 미매칭 → 과치환 회피. か/さ/な 등 단어 시작 흔한
# 1자 조사는 제외(誘拐 등 오매칭 방지) — 안전한 は が を に へ と も の 만.
_CJK_NAME_TAIL = (
    r"(?=$|[\s、。，．・…！？!?「」『』（）()〜ー～]"
    r"|は|が|を|に|へ|と|も|の|ちゃん|ちゃま|くん|さん|さま|様|氏)"
)


# 받침 의존 조사(파티클). longest-first(이라고>라고, 이야>이, 이가>이/가, 이랑>랑, 이나>나).
_JOSA_ALT = "이라고|라고|이야|이가|이랑|이나|아|야|이|가|은|는|을|를|과|와|랑|나"
_RENDER_RE = re.compile(_TOK_RE + r"(" + _JOSA_ALT + r")?")


def _batchim(s: str) -> bool:
    """마지막 글자에 받침이 있나(한글만)."""
    if not s:
        return False
    last = s[-1]
    return "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 != 0


def _apply_josa(nick: str, j: str) -> str:
    """현재 이름 + 받침 맞춘 조사. 비한글 이름은 조사 미부착(greetings 계열과 일관, SOMA-347)."""
    if not nick or not ("가" <= nick[-1] <= "힣"):
        return nick  # 라틴 등 비한글: 'Alex는'·'Alex가' 방지 — 인라인 조사도 일괄 차단
    if j in ("아", "야"):
        return greetings.vocative(nick)
    if j == "이야":
        return greetings.copula(nick)
    if j in ("이", "가"):
        return greetings.subject(nick)
    if j in ("과", "와"):
        return greetings.with_wa(nick)
    if j in ("이라고", "라고"):
        return greetings.quote_ira(nick)
    b = _batchim(nick)
    if j == "이가":  # 구어 주격: 받침이면 이름+이가, 아니면 이름+가
        return nick + ("이가" if b else "가")
    if j in ("은", "는"):
        return nick + ("은" if b else "는")
    if j in ("을", "를"):
        return nick + ("을" if b else "를")
    if j in ("이랑", "랑"):
        return nick + ("이랑" if b else "랑")
    if j in ("이나", "나"):
        return nick + ("이나" if b else "나")
    return nick + j  # 도달 안 함


def to_placeholder(text: str | None, nickname: str | None) -> str | None:
    """이름 스템 → `{유저이름}` 마스킹. 앞이 한글/라틴계면 미매칭(국'민' 오염 방지).

    뒤 경계는 이름 스크립트에 따라 다르다:
    - 한글로 끝나는 이름: 조사(아/야·이가…)가 바로 붙으므로 뒤 경계 없음('승민아'→'{}아').
    - 라틴계로 끝나는 이름: 단어 중간 매칭을 막아 오염 방지('Ann'이 'Anniversary'를, 'May'가
      'Maybe'를 마스킹하지 않음). SOMA-347.
    자연 멱등: 재실행 시 이미 이름이 토큰으로 바뀌어 있어 매칭될 실명이 없다(no-op).
    """
    if not text or not nickname:
        return text
    # NFC 통일 — 유저 입력이 분해형(NFD, iOS/macOS)이면 프로필(NFC)과 안 맞아 실명이 안 잡힌다.
    text = unicodedata.normalize("NFC", text)
    nick = unicodedata.normalize("NFC", nickname)
    # CJK(한자·가나) 이름은 안전장치를 갖춰 마스킹한다(SOMA-365 후속, ja 이름 평문저장 방지):
    # 2글자+ & 앞 경계(단어 꼬리 아님) & 뒤가 문장경계·부호·안전 조사·경칭일 때만. 1글자·단어연속
    # (愛⊂恋愛·さくら⊂さくらんぼ)은 미매칭 → 과치환 회피. 라운드트립은 render가 토큰→현재이름 +
    # 조사/부호 리터럴 유지로 보존한다(개명 시 흔치 않은 접두 겹침만 미용 손상).
    if _CJK_RE.search(nick):
        if len(nick) < 2:
            return text  # 1글자 CJK 이름은 과치환 위험 커 스킵(기밀선=RLS+mem0 이름미저장)
        # 앞 경계는 라틴/한글만 차단(CJK는 허용 — 일본어는 띄어쓰기가 없어 문장 중간 이름이 항상
        # 가나/한자 뒤에 온다. 접두 겹침 과치환은 render 라운드트립으로 무해, 개명 시만 미용 손상).
        return re.sub(rf"(?<![{_LATIN}가-힣]){re.escape(nick)}{_CJK_NAME_TAIL}", TOKEN, text)
    trailing = rf"(?![{_LATIN}])" if _LATIN_RE.match(nick[-1]) else ""
    return re.sub(rf"(?<![가-힣{_LATIN}]){re.escape(nick)}{trailing}", TOKEN, text)


def render(text: str | None, nickname: str | None) -> str | None:
    """`{유저이름}`(+조사) → 현재 이름(+받침 맞춘 조사). 토큰 없으면 그대로(옛 리터럴 텍스트 통과)."""
    if not text:
        return text
    text = unicodedata.normalize("NFC", text)
    if TOKEN not in text:
        return text
    n = unicodedata.normalize("NFC", nickname) if nickname else "너"

    def _repl(m: re.Match) -> str:
        j = m.group(1)
        if not j:
            return n
        # 조사 뒤가 한글이면 파티클이 아니라 단어 일부(승민'아파트') → 이름 + 리터럴 조사.
        after = text[m.end()] if m.end() < len(text) else ""
        if "가" <= after <= "힣":
            return n + j
        return _apply_josa(n, j)

    return _RENDER_RE.sub(_repl, text)
