"""일본어 정식 지원 — 서버 고정문구 표면(웰컴일기·LLM 언어규칙) (SOMA-361).

경로 A(정적 카피)는 위 test_greetings/notify/subscription/i18n에서, 여기선 웰컴일기와
경로 B(프롬프트 언어규칙)의 ja 분기를 검증한다. 핵심: ja는 한자·가나 허용, 한글만 배제.
"""
from app.services import diary, diary_prompts, prompts


def test_welcome_diary_language_buckets():
    assert diary._welcome_content("ko") == diary._WELCOME_CONTENT
    assert diary._welcome_content("ja") == diary._WELCOME_CONTENT_JA
    assert diary._welcome_content("en") == diary._WELCOME_CONTENT_EN
    assert diary._welcome_content("zh") == diary._WELCOME_CONTENT_EN  # 미지원 → en 폴백
    assert "{유저이름}" in diary._WELCOME_CONTENT_JA  # egress 렌더용 placeholder 보존(토큰 자체는 한글)
    # placeholder 토큰을 뺀 본문에는 한글이 없어야 한다(일본어 순수성).
    body = diary._WELCOME_CONTENT_JA.replace("{유저이름}", "")
    assert not any("가" <= c <= "힣" for c in body)


def test_chat_system_prompt_ja_allows_kanji_forbids_hangul():
    sp = prompts.system_prompt("ja")
    assert "Chinese characters" not in sp   # ja는 한자 금지 안 함(정상 일본어 보존, SOMA-361 C2)
    assert "ハングル" in sp                   # 한글 혼입만 금지
    assert "Chinese characters" in prompts.system_prompt("en")  # en 회귀 없음
    assert "한국어" in prompts.system_prompt("ko")               # ko 회귀 없음


def test_diary_prompt_ja_allows_kanji_keeps_weather_header():
    dp = diary_prompts.diary_prompt("ja", "Ken")
    assert "Chinese characters" not in dp
    assert "ハングル" in dp
    assert "Weather:" in dp  # parse()가 값(enum) 기준으로 인식하도록 영어 날씨 헤더 유지
    assert "Chinese characters" in diary_prompts.diary_prompt("en", "Sam")  # en 회귀 없음
