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


# --- 콘텐츠 언어는 ko·en·ja 셋뿐 ---
#
# 예전에는 LLM 지시에 원본 언어 태그를 그대로 넣어서 중국어 유저는 중국어로, 태국어 유저는
# 태국어로 답을 받았다. 그런데 푸시와 서버 문구는 i18n.resolve 폴백으로 이미 영어라, 한 유저가
# 화면마다 다른 언어를 봤다. 페르소나·말투·검수 프롬프트도 셋만 준비돼 있어 나머지는 품질을
# 보장할 수 없다. 이제 ko·ja가 아니면 전부 영어다.
_UNSUPPORTED = ("zh-Hant-TW", "es-ES", "th", "fr", "vi")


def test_chat_falls_back_to_english_outside_ko_ja():
    for lang in _UNSUPPORTED:
        sp = prompts.system_prompt(lang)
        assert "reply only in English." in sp, f"{lang}: 영어로 안 떨어진다"
        # 태그 조각이 영어 단어에 우연히 들어갈 수 있어(th -> the) 지시문 형태로 정확히 본다.
        assert f"in {lang}." not in sp, f"{lang}: 원본 언어 태그가 프롬프트에 새어 들어갔다"


def test_diary_falls_back_to_english_outside_ko_ja():
    for lang in _UNSUPPORTED:
        dp = diary_prompts.diary_prompt(lang, "Kim")
        assert " in en." in dp, f"{lang}: 일기가 영어로 안 떨어진다"
        assert f"in {lang}." not in dp, f"{lang}: 원본 언어 태그가 일기 프롬프트에 새어 들어갔다"


def test_supported_languages_are_unchanged():
    """ko·ja는 그대로여야 한다. 지역 태그가 붙어도 같은 버킷으로 간다."""
    assert "반드시 한국어" in prompts.system_prompt("ko-KR")
    assert "日本語" in prompts.system_prompt("ja-JP")
    assert "reply only in English." in prompts.system_prompt("en-US")


# --- 영어 네이티브 페르소나 ---
#
# 예전에는 영어 유저도 한국어로 쓰인 페르소나를 받았다. 말투 규칙이 한국어 기준이라 영어에
# 맞지 않았고, 실제 응답이 축약형을 하나도 안 써서(It is / do not) 딱딱했다. 일본어와 같은
# 방식으로 영어 원본을 따로 둔다.
def test_english_persona_has_no_korean_or_kana():
    import re

    sp = prompts.system_prompt("en")
    assert not re.search(r"[가-힣]", sp), "영어 프롬프트에 한글이 남아 있다"
    assert not re.search(r"[぀-ヿ]", sp), "영어 프롬프트에 가나가 남아 있다"


def test_english_persona_is_used_for_unsupported_languages():
    """미지원 언어도 영어 갈래로 온다 — 한국어 페르소나로 떨어지면 안 된다."""
    for lang in ("zh-Hant-TW", "th", "es-ES"):
        assert prompts.system_prompt(lang) == prompts.system_prompt("en")


def test_english_persona_keeps_the_capi_rules():
    """언어가 달라도 지켜야 할 규칙은 같다. 옮기면서 빠지면 안 된다."""
    sp = prompts.system_prompt("en")
    for rule, why in [
        ("Cappy", "이름"),
        ("Don't use commas", "쉼표 금지"),
        ("Use contractions", "축약형"),
        ("three lines", "세 줄 상한"),
        ("emoji", "이모지 금지"),
        ("capybara", "정체성"),
        ("diary", "일기는 비밀"),
    ]:
        assert rule in sp, f"영어 페르소나에 {why} 규칙이 없다"


def test_ko_and_ja_personas_are_untouched():
    assert "너는 '캐피'야" in prompts.system_prompt("ko")
    assert "\u30ad\u30e3\u30d4\u30fc" in prompts.system_prompt("ja")


# --- 안전 규칙은 세 언어에서 모두 살아 있어야 한다 ---
#
# 페르소나를 새로 쓰거나 손볼 때 제일 위험한 건 위기 대응 절이 조용히 빠지는 것이다.
# 앞의 `test_english_persona_keeps_the_capi_rules`는 말투·부호만 봐서 이 절을 통째로
# 지워도 통과했다(점검에서 확인). 세 언어 각각을 여기서 고정한다.
#
# 규칙 자체는 [[persona-prompt-rules]] 4번(2026-08-01 사용자 확정)이다. 캐피는 친구로만
# 남는다 — 기관·상담전화 안내를 하지 않고, 애매한 넋두리를 위기로 몰지 않으며, 대화를
# 닫거나 쉬라며 보내지 않는다.
_SAFETY_MARKERS = {
    "ko": [
        ("해치려는", "지금 당장 자해하려는 경우만 위기로 본다"),
        ("위기로 몰지", "애매한 넋두리를 위기로 몰지 않는다"),
        ("기관 전화번호", "기관·상담전화 안내 금지"),
        ("안전 얘기를 다시 꺼내지", "안전을 두 번 묻지 않는다"),
        ("대화를 닫거나", "대화를 닫거나 보내지 않는다"),
    ],
    "ja": [
        ("\u50b7\u3064\u3051\u305d\u3046", "지금 당장 자해하려는 경우만"),
        ("\u5371\u6a5f\u3068\u53d7\u3051\u53d6\u3089\u305a", "애매한 말을 위기로 몰지 않는다"),
        ("\u96fb\u8a71\u756a\u53f7\u306e\u6848\u5185", "상담창구 전화번호 안내 금지"),
        ("\u5b89\u5168\u306e\u8a71\u3092\u84b8\u3057\u8fd4\u3055\u305a", "안전을 두 번 묻지 않는다"),
        ("\u4f1a\u8a71\u3092\u9589\u3058\u305f\u308a", "대화를 닫거나 보내지 않는다"),
    ],
    "en": [
        ("hurt themselves", "지금 당장 자해하려는 경우만"),
        ("is not a crisis", "애매한 넋두리를 위기로 몰지 않는다"),
        ("hotline numbers", "기관·상담전화 안내 금지"),
        ("Don't raise safety again", "안전을 두 번 묻지 않는다"),
        ("Don't close the conversation", "대화를 닫거나 보내지 않는다"),
    ],
}


def test_crisis_rules_survive_in_every_language():
    for lang, markers in _SAFETY_MARKERS.items():
        sp = prompts.system_prompt(lang)
        for marker, why in markers:
            assert marker in sp, f"{lang} 페르소나에서 안전 규칙이 빠졌다 — {why}"


def test_unsupported_languages_get_the_crisis_rules_too():
    """미지원 언어도 영어 페르소나를 받으므로 안전 규칙이 함께 온다."""
    for lang in ("zh-Hant-TW", "th", "es-ES"):
        sp = prompts.system_prompt(lang)
        for marker, why in _SAFETY_MARKERS["en"]:
            assert marker in sp, f"{lang}에 안전 규칙이 없다 — {why}"


def test_no_hotline_guidance_in_any_language():
    """기관 안내 금지가 규칙인데 페르소나가 거꾸로 번호를 예시로 들면 안 된다."""
    import re

    for lang in ("ko", "ja", "en"):
        sp = prompts.system_prompt(lang)
        assert not re.search(r"\b1(09|39|577)[-\s]?\d{3,4}\b", sp), (
            f"{lang} 페르소나에 상담전화 번호가 들어 있다"
        )
