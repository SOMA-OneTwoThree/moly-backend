"""부호 정제 공용 util — 마크다운·말줄임표·대시 제거, 허용부호·이름토큰 보존."""
from app.services import text_clean


def test_strip_markdown_bold_and_dash():
    assert text_clean.strip_symbols("**강조** 텍스트") == "강조 텍스트"
    assert text_clean.strip_symbols("- 리스트 항목") == "리스트 항목"
    assert text_clean.strip_symbols("밑줄_표시_ 물결~강조") == "밑줄 표시 물결 강조"


def test_strip_ellipsis():
    assert text_clean.strip_symbols("그래서... 그랬어") == "그래서 그랬어"
    assert text_clean.strip_symbols("음… 글쎄") == "음 글쎄"
    assert text_clean.strip_symbols("어..") == "어"


def test_preserves_allowed_punct():
    assert text_clean.strip_symbols("정말? 응! 그래.") == "정말? 응! 그래."
    # 부호 앞 공백 정리
    assert text_clean.strip_symbols("정말 ?") == "정말?"


def test_preserves_name_token():
    # 중괄호·한글은 STRAY 대상이 아님 → 이름 placeholder 토큰 안전
    assert text_clean.strip_symbols("{유저이름}아 안녕") == "{유저이름}아 안녕"
    out = text_clean.strip_symbols("**오** {유저이름}이가 왔어")
    assert "{유저이름}이가" in out and "**" not in out


def test_none_and_empty():
    assert text_clean.strip_symbols("") == ""
    assert text_clean.strip_symbols(None) is None


def test_strip_symbols_removes_junk_chars():
    # 깨진 문자(U+FFFD)는 앞뒤 글자를 재결합하며 제거 (메�뉴 → 메뉴)
    assert text_clean.strip_symbols("저녁 메�뉴 얘기부터") == "저녁 메뉴 얘기부터"
    assert text_clean.strip_symbols("오늘​은 좋았다") == "오늘은 좋았다"   # 제로폭 ZWSP
    assert text_clean.strip_symbols("﻿오늘 좋았다") == "오늘 좋았다"        # BOM
    assert text_clean.strip_symbols("오늘‏ 좋았다") == "오늘 좋았다"        # bidi RLM
    assert text_clean.strip_symbols("제어\x07문자") == "제어문자"               # C0 제어
    assert text_clean.strip_symbols("오늘 좋았다") == "오늘 좋았다"         # NBSP → 공백
# --- 외래문자 백스톱 — 응답 언어에 없어야 할 계열의 글자 탐지/제거 ---
#
# 실제로 새어 나간 4건(dev 캐피 발화 178건 중)은 전부 완결된 문장 뒤에 붙은 꼬리였고,
# 계열도 제각각이었다(그리스 구자라트 키릴). 예전 코드는 지울 계열을 나열하는 방식이라
# 목록에 없던 이 세 계열이 그대로 통과했다. 그래서 아래는 계열별로 하나씩 고정한다.
def test_detects_any_script_outside_the_response_language():
    ko = dict(language="ko")
    assert text_clean.has_foreign("느긋하게 있어. \u03a3", **ko) is True          # 그리스(실측 62)
    assert text_clean.has_foreign("몰라.\u0ac7\u0aa3", **ko) is True             # 구자라트(실측 265)
    assert text_clean.has_foreign("짙어 보여. \u04bb\u04af", **ko) is True       # 키릴(실측 353)
    assert text_clean.has_foreign("\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35 왔어", **ko) is True  # 태국(목록에 없던 계열)
    assert text_clean.has_foreign("나도 中 생각엔", **ko) is True                  # 한자
    assert text_clean.has_foreign("완전 かわいい다", **ko) is True                  # 가나
    assert text_clean.has_foreign("\U00020000 희귀자", **ko) is True               # CJK 확장 B


def test_no_false_positive_on_normal_korean():
    ko = dict(language="ko")
    assert text_clean.has_foreign("오늘 좀 어땠어 힘들었어?", **ko) is False
    assert text_clean.has_foreign("아이폰 3시에 iPhone 봤어", **ko) is False   # 라틴 숫자
    assert text_clean.has_foreign("카페에서 caf\u00e9 마셨어", **ko) is False  # 악센트 라틴
    assert text_clean.has_foreign("{유저이름}아 안녕", **ko) is False           # placeholder 안전
    assert text_clean.has_foreign("", **ko) is False
    assert text_clean.has_foreign(None, **ko) is False


def test_allowed_scripts_differ_by_language():
    """일본어 응답의 가나 한자는 정상이다. 예전엔 한국어에만 백스톱이 있어 ja en은 무방비였다."""
    assert text_clean.has_foreign("今日はどうだった", language="ja") is False
    assert text_clean.has_foreign("今日はどうだった \u03a3", language="ja") is True
    assert text_clean.has_foreign("How was your day", language="en") is False
    assert text_clean.has_foreign("How was your day \u04bb", language="en") is True
    assert text_clean.has_foreign("Nice jalape\u00f1o d\u00eda", language="en") is False


def test_fullwidth_and_halfwidth_forms_are_not_stripped():
    """전각 라틴·반각 가타카나는 정상 글자다. 빼놓으면 멀쩡한 일본어 글을 지운다."""
    assert text_clean.has_foreign("\uff21\uff22\uff23 test", language="en") is False  # 전각 라틴
    assert text_clean.has_foreign("\uff21\uff22 시작", language="ko") is False
    assert text_clean.has_foreign("\uff83\uff7d\uff84\u3067\u3059", language="ja") is False  # 반각 가타카나


def test_strip_removes_and_normalizes():
    ko = dict(language="ko")
    assert text_clean.strip_foreign("나도 中 생각엔", **ko) == "나도 생각엔"
    assert text_clean.strip_foreign("완전 かわいい다", **ko) == "완전 다"
    assert text_clean.strip_foreign("느긋하게 있어. \u03a3", **ko) == "느긋하게 있어."
    assert text_clean.strip_foreign("", **ko) == ""


def test_strip_removes_combining_marks_too():
    """모음기호는 유니코드가 '글자'로 분류하지 않는다. 글자만 지우면 그 조각이 남는다.

    실측 'વાત'에서 글자만 지우면 'ા'가 남아 오히려 더 이상해졌다.
    """
    assert text_clean.strip_foreign("알아준 것 같아. \u0ab5\u0abe\u0aa4?", language="ko") == "알아준 것 같아. ?"


def test_nickname_is_never_stripped():
    """닉네임은 유저가 정한 값이라 응답 언어와 계열이 달라도 정상이다.

    이름이 사라진 답을 내보내는 쪽이 이물질이 남는 것보다 나쁘다.
    """
    nick = "\u0410\u043d\u044f"  # 키릴 이름
    assert text_clean.has_foreign(f"{nick}야 안녕", language="ko", keep=nick) is False
    assert text_clean.strip_foreign(f"{nick}야 안녕", language="ko", keep=nick) == f"{nick}야 안녕"
    # 이름은 지키면서 진짜 이물질은 그대로 지운다
    assert text_clean.strip_foreign(f"{nick}야 안녕 \u03a3", language="ko", keep=nick) == f"{nick}야 안녕"


def test_unmodeled_language_is_left_alone():
    """허용 계열을 안 적은 언어는 아예 거르지 않는다 — 본문이 통째로 지워지는 쪽이 더 나쁘다."""
    assert text_clean._filter_for("zh-Hant-TW")[0] is True  # 미지원은 en 폴백이라 걸러짐
    assert set(text_clean._ALLOWED_LETTERS) == {"ko", "en", "ja"}, (
        "i18n.SUPPORTED에 언어를 추가했으면 여기 허용 계열도 적어야 한다"
    )


# --- 메타 프리앰블 제거 — 한국어 응답 앞 라틴 메타 백스톱(SOMA-329) ---
def test_strip_leading_meta_real_english_leak():
    # 실측 msg 5293 — 영어 메타 프리앰블 + 정상 한국어. 첫 한글부터 남긴다.
    leak = (
        "momentary pause here this is a serious moment that requires immediate, "
        "direct attention, not a casual continuation of the previous conversational "
        "thread. 내가 지금 곁에 있어. 무슨 일 있었는지 말해줄래?"
    )
    assert text_clean.strip_leading_meta(leak) == "내가 지금 곁에 있어. 무슨 일 있었는지 말해줄래?"


def test_strip_leading_meta_real_spanish_leak():
    # 실측 msg 4760 — 스페인어 메타(구조적 탐지라 언어 불문). 첫 한글부터 남긴다.
    leak = (
        "Ha pillado ese mensaje pero no le veo relación con la charla, seguramente "
        "sea un fallo. Te contesto en mi papel normal. Ah, 급류 그거 그냥 오타로 보낸 건 줄 알았어."
    )
    assert text_clean.strip_leading_meta(leak) == "급류 그거 그냥 오타로 보낸 건 줄 알았어."


def test_strip_leading_meta_bracket_and_note_forms():
    assert (
        text_clean.strip_leading_meta("Note: this requires a careful direct response now. 지금 많이 힘든 거지.")
        == "지금 많이 힘든 거지."
    )
    assert (
        text_clean.strip_leading_meta("[content warning] the user is expressing suicidal ideation here. 나 여기 있어.")
        == "나 여기 있어."
    )


def test_strip_leading_meta_no_false_positive_on_short_latin():
    # 짧은 토큰·고유명사(라틴 단어 < 4)는 미발동 — 정상 응답 보존.
    for s in [
        "무슨 일이야. 나 여기 있어.",          # 한글로 시작
        "LP 듣는 게 요즘 낙이야.",             # 1단어
        "AI 아니야 나 그냥 캐피야.",            # 1단어
        "BTS 콘서트 갔다 왔어?",              # 1단어
        "New York 얘기구나 거기 가봤어?",      # 2단어 고유명사
        "OK 그래 알겠어.",                    # 1단어
    ]:
        assert text_clean.strip_leading_meta(s) == s


def test_strip_leading_meta_no_false_positive_on_english_titles():
    # 라틴 단어 4+이라도 구두점(마침표·콜론) 없는 제목·가사는 미발동 — 메타 문장과 구분.
    for s in [
        "Red Velvet Queendom Special Clip 봤어?",   # 5단어 제목
        "The Lord of the Rings 다시 보고 싶다",       # 5단어 제목
        "Let it go let it go 라는 노래 알아?",         # 5단어 가사
    ]:
        assert text_clean.strip_leading_meta(s) == s


def test_strip_leading_meta_edge_cases():
    assert text_clean.strip_leading_meta("") == ""
    assert text_clean.strip_leading_meta(None) is None
    # 한글 없는 응답(언어 실패 등)은 손대지 않는다 — 전량삭제 방지(fail-safe).
    assert text_clean.strip_leading_meta("I cannot help with that.") == "I cannot help with that."
    # 라틴 뒤 본문이 비면 원문 유지.
    assert text_clean.strip_leading_meta("this is only meta with no korean body") == (
        "this is only meta with no korean body"
    )


# --- 호출부 배선 고정 ---
#
# 위 테스트들은 text_clean만 본다. 그래서 chat.py가 백스톱을 한국어에만 걸도록 되돌아가도
# 전부 통과한다. 원래 결함이 정확히 "적용 범위"에 있었으므로 여기서 고정한다.
def test_chat_egress_backstop_is_not_korean_only():
    """외래문자 백스톱은 언어를 가리지 않아야 한다.

    엉뚱한 언어 글자가 문장 뒤에 붙는 건 영어·일본어 응답에서도 똑같이 일어난다.
    예전에는 `is_ko and` 게이팅이라 ja·en 응답은 백스톱이 아예 없었다.
    """
    import inspect

    from app.services import chat

    line = next(
        ln for ln in inspect.getsource(chat).splitlines() if "has_foreign(reply_text" in ln
    )
    assert "is_ko" not in line, "백스톱이 다시 한국어 전용이 됐다 — ja·en 응답이 무방비다"


def test_chat_egress_passes_nickname_as_keep():
    """닉네임을 안 넘기면 한글 밖 이름이 응답에서 통째로 지워진다."""
    import inspect

    from app.services import chat

    src = inspect.getsource(chat)
    for call in ("has_foreign(reply_text", "strip_foreign(reply_text"):
        line = next(ln for ln in src.splitlines() if call in ln)
        assert "keep=nick" in line, f"{call}에 닉네임을 안 넘긴다"
