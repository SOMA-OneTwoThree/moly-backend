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


def test_chat_system_prompt_ja_uses_native_persona():
    # 일본 출시: ja는 한국어 번역이 아니라 네이티브 페르소나(이름 キャピー·1인칭 ぼく·タメ口, 정유환 지정).
    sp = prompts.system_prompt("ja")
    assert "キャピー" in sp and "ぼく" in sp          # 지정 이름·1인칭
    assert "[물음표" not in sp                        # 한국어 물음표 강제 미포함(일본어는 か/の가 처리)
    assert "写真" in sp                               # 없는 기능(사진 등) 언급 금지 규칙 포함(SOMA-351)
    # 네이티브 = 페르소나 본문에 한글 0(한국어 페르소나 미사용).
    assert not any("가" <= c <= "힣" for c in prompts.CAPI_PERSONA_JA)
    assert "[물음표" in prompts.system_prompt("ko")   # ko는 물음표 강제 유지(회귀 없음)


def test_chat_system_prompt_forbids_absent_features_both_langs():
    # SOMA-351: 사진·음성 등 없는 기능 언급 금지 규칙이 한/일 페르소나 모두에.
    ko = prompts.system_prompt("ko")
    assert "사진" in ko and "영상" in ko
    ja = prompts.system_prompt("ja")
    assert "写真" in ja and "動画" in ja


def test_diary_prompt_ja_allows_kanji_keeps_weather_header():
    dp = diary_prompts.diary_prompt("ja", "Ken")
    assert "Chinese characters" not in dp
    assert "ハングル" in dp
    assert "Weather:" in dp  # parse()가 값(enum) 기준으로 인식하도록 영어 날씨 헤더 유지
    assert "Chinese characters" in diary_prompts.diary_prompt("en", "Sam")  # en 회귀 없음


# --- 캐릭터 이름의 라틴 표기 ---
#
# 영어(와 지원 밖 언어)는 한국어 페르소나를 그대로 쓰므로 이름이 '캐피'로만 적혀 있었다.
# 그러면 모델이 라틴 표기를 지어낸다 — dev 실측에서 "My name is Capi. C A P I."라고 답했고
# "Capi or Cappy?"에는 "Capi. Just one p."로 Cappy를 부정했다. 푸시 알림은 이미 'Cappy'로
# 나가므로(notify.py) 유저는 Cappy에게 알림을 받고 들어와 Capi와 대화하게 된다.
def test_latin_name_is_pinned_for_non_ko_ja():
    for lang in ("en", "en-US", "zh-Hant-TW", "es"):
        sp = prompts.system_prompt(lang)
        assert "Cappy" in sp, f"{lang}: 라틴 이름이 프롬프트에 없다 — 모델이 지어낸다"


def test_latin_name_matches_push_surface():
    """대화와 푸시가 같은 이름을 써야 한다. 어긋나면 유저가 다른 상대로 읽는다."""
    from app.services import notify

    assert notify._MORNING["en"][0] in prompts.system_prompt("en")


def test_ko_and_ja_keep_their_own_names():
    """라틴 이름을 못 박은 게 ko·ja로 새면 안 된다."""
    assert "Cappy" not in prompts.system_prompt("ko")
    assert "Cappy" not in prompts.system_prompt("ja")
    assert "\u30ad\u30e3\u30d4\u30fc" in prompts.system_prompt("ja")
