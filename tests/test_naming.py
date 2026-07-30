"""닉네임 스템 마스킹 — 마스킹·라운드트립·개명 조사교정·단어경계·과치환·리터럴통과·폴백·NFC."""
import unicodedata

import pytest

from app.services import naming

T = naming.TOKEN  # "{유저이름}"


# --- to_placeholder: 이름 스템만 마스킹, 조사는 리터럴 유지(열거 불필요) ---
@pytest.mark.parametrize(
    "text, expect",
    [
        ("승민아 안녕", f"{T}아 안녕"),
        ("승민이가 그랬어", f"{T}이가 그랬어"),        # 구어 주격 — 구 방식이 놓치던 케이스
        ("승민이 왔어", f"{T}이 왔어"),
        ("승민이야", f"{T}이야"),
        ("승민씨 안녕하세요", f"{T}씨 안녕하세요"),      # 씨 — 조사표 없이도 마스킹됨
        ("승민님", f"{T}님"),
        ("승민의 하루", f"{T}의 하루"),
        ("승민한테 말했어", f"{T}한테 말했어"),
        ("승민을 봤어", f"{T}을 봤어"),
        ("오 승민, 오랜만", f"오 {T}, 오랜만"),
        ("이름 안 나오는 문장", "이름 안 나오는 문장"),   # 미사용 → 그대로
    ],
)
def test_mask_stem_keeps_josa_literal(text, expect):
    assert naming.to_placeholder(text, "승민") == expect


# --- render: 같은 이름 라운드트립(원복) ---
@pytest.mark.parametrize(
    "text",
    [
        "승민아 안녕",
        "승민이가 그랬어",
        "승민이 왔어",
        "승민이야",
        "승민씨 안녕",
        "승민의 하루가 어땠어",
        "승민한테 말했어",
        "승민을 봤어",
        "오 승민, 반가워",
    ],
)
def test_roundtrip_same_name(text):
    assert naming.render(naming.to_placeholder(text, "승민"), "승민") == text


# --- render: 개명 시 조사까지 교정(받침 있는 승민 → 없는 지호) ---
@pytest.mark.parametrize(
    "stored, expect",
    [
        (f"{T}아 안녕", "지호야 안녕"),          # 호격 아→야
        (f"{T}이 왔어", "지호가 왔어"),          # 주격 이→가
        (f"{T}이가 그랬어", "지호가 그랬어"),     # 구어 주격 이가→가
        (f"{T}이야", "지호야"),                  # 서술격 이야→야
        (f"{T}은 어때", "지호는 어때"),          # 보조사 은→는
        (f"{T}을 봤어", "지호를 봤어"),          # 목적격 을→를
        (f"{T}과 놀자", "지호와 놀자"),          # 동반 과→와
        (f"{T}이랑 가자", "지호랑 가자"),        # 이랑→랑
        (f"{T}씨 안녕", "지호씨 안녕"),          # 받침무관(씨) — 리터럴 그대로
        (f"{T}의 하루", "지호의 하루"),          # 받침무관(의) — 그대로
    ],
)
def test_rename_josa_corrected(stored, expect):
    assert naming.render(stored, "지호") == expect


def test_rename_to_batchim_name():
    # 받침 있는 이름으로 개명 → 받침형 조사
    assert naming.render(f"{T}아 안녕", "성민") == "성민아 안녕"
    assert naming.render(f"{T}이가 그랬어", "성민") == "성민이가 그랬어"


# --- 단어 경계: 조사처럼 생겼지만 뒤에 한글이 이어지면 리터럴(과치환 방지) ---
def test_word_boundary_not_josa():
    assert naming.render(f"{T}아파트에서", "지호") == "지호아파트에서"   # 아파트, not 아
    assert naming.render(f"{T}은행 갔어", "지호") == "지호은행 갔어"     # 은행, not 은


# --- 과치환 방지: 이름이 다른 단어의 일부면 미마스킹 ---
def test_no_overmatch_prefix_hangul():
    assert naming.to_placeholder("국민 여러분", "민") == "국민 여러분"     # 국'민'
    assert naming.to_placeholder("김승민 아니야", "승민") == "김승민 아니야"  # 김'승민'


# --- 리터럴 통과(옛 데이터 안전): 토큰 없으면 그대로 ---
def test_render_passes_literal_through():
    assert naming.render("그냥 옛날 텍스트야", "지호") == "그냥 옛날 텍스트야"
    assert naming.render("승민아 (옛 리터럴)", "지호") == "승민아 (옛 리터럴)"


# --- 멱등: 재실행해도 이름이 이미 토큰이라 no-op ---
def test_idempotent():
    once = naming.to_placeholder("승민아 안녕", "승민")
    assert naming.to_placeholder(once, "승민") == once


# --- None/빈값/닉네임 없음 ---
def test_none_and_empty():
    assert naming.to_placeholder(None, "승민") is None
    assert naming.to_placeholder("승민아", None) == "승민아"   # 닉네임 없으면 그대로
    assert naming.render(None, "지호") is None
    assert naming.render("일반 텍스트", None) == "일반 텍스트"


def test_render_none_nickname_fallback():
    # 닉네임 없으면 폴백('너')으로 — 크래시만 안 나면 됨
    out = naming.render(f"{T}아 안녕", None)
    assert "너" in out and T not in out


# --- 비한글 이름(영문 등) ---
def test_non_korean_name():
    stored = naming.to_placeholder("Alex 안녕", "Alex")
    assert stored == f"{T} 안녕"
    assert naming.render(stored, "Alex") == "Alex 안녕"


def test_apply_josa_non_korean_no_particle():
    # 비한글 이름은 인라인 조사(은/는·이가·를 등)도 미부착 — greetings 계열과 일관(SOMA-347 정리).
    assert naming._apply_josa("Alex", "은") == "Alex"
    assert naming._apply_josa("Alex", "이가") == "Alex"
    assert naming._apply_josa("Alex", "를") == "Alex"
    # 한글 이름은 받침 맞춰 조사 유지(회귀 없음).
    assert naming._apply_josa("승민", "은") == "승민은" and naming._apply_josa("지호", "를") == "지호를"
    # render 경로: 개명(한글→라틴) 시 저장된 조사가 어색하게 붙지 않음.
    assert naming.render(f"{T}은 어때", "Alex") == "Alex 어때"


def test_latin_name_word_boundary_no_overmask():
    # 라틴계 이름은 단어 중간을 마스킹하지 않는다(SOMA-347).
    assert naming.to_placeholder("Anniversary party", "Ann") == "Anniversary party"  # Ann≠Anniversary
    assert naming.to_placeholder("Maybe later", "May") == "Maybe later"              # May≠Maybe
    assert naming.to_placeholder("Hi Ann!", "Ann") == f"Hi {T}!"                     # 독립 언급은 마스킹
    assert naming.to_placeholder("Ann's book", "Ann") == f"{T}'s book"               # 소유격 경계
    # 한글 이름은 뒤 경계 없이 조사 바로 뒤까지 마스킹(기존 동작 유지).
    assert naming.to_placeholder("승민아 안녕", "승민") == f"{T}아 안녕"


def test_nfd_input_is_masked():
    # 유저가 분해형(NFD, iOS/macOS)으로 자기 이름을 쳐도 마스킹된다(프로필=NFC 가정).
    nfd = unicodedata.normalize("NFD", "승민아 안녕")
    out = naming.to_placeholder(nfd, "승민")
    assert naming.TOKEN in out
    assert "승민" not in unicodedata.normalize("NFC", out)  # 실명 스템 잔존 없음
    assert naming.render(out, "지호") == "지호야 안녕"


def test_vietnamese_extended_latin_boundary():
    # 베트남 확장라틴(U+1E00~)·악센트 이름도 라틴 단어경계가 적용된다(SOMA-365).
    assert naming.to_placeholder("Tuệ went home", "Tuệ") == f"{T} went home"
    assert naming.to_placeholder("Tuệt", "Tuệ") == "Tuệt"          # 더 긴 라틴 단어 속 과치환 방지
    assert naming.to_placeholder("José는 왔어", "José") == f"{T}는 왔어"  # 악센트 라틴 + 한글 조사 뒤


def test_cjk_name_not_masked():
    # CJK 마스킹은 과치환 안전장치가 있다(SOMA-365 후속): 1글자·단어연속(뒤가 조사가 아닌 일반 가나/한자)은
    # 미매칭 → 愛⊂恋愛·さくら⊂さくらんぼ·健太⊂健太郎 과치환 회피.
    assert naming.to_placeholder("恋愛について話した", "愛") == "恋愛について話した"   # 1글자 스킵
    assert naming.to_placeholder("さくらんぼ", "さくら") == "さくらんぼ"              # 뒤 'ん'=조사 아님
    assert naming.to_placeholder("愛と話した", "愛") == "愛と話した"                  # 1글자 스킵
    assert naming.to_placeholder("健太郎が来た", "健太") == "健太郎が来た"            # 뒤 '郎'=한자 연속
    assert naming.to_placeholder("ゆうかいされた", "ゆう") == "ゆうかいされた"        # 뒤 'か'=제외 조사(誘拐 안전)
    # 라틴/한글 이름은 계속 마스킹(회귀 없음).
    assert naming.TOKEN in naming.to_placeholder("Alex came", "Alex")
    assert naming.TOKEN in naming.to_placeholder("승민이 왔어", "승민")


def test_cjk_name_masked_with_boundary():
    # 2글자+ CJK 이름이 안전 조사(は が を に へ と も の)·경칭·문장부호·경계와 함께면 마스킹
    # (SOMA-365 후속 — 일본어 이름 평문저장 방지). 실측서 캐피가 'まおの…'처럼 이름+조사로 부름.
    T = naming.TOKEN
    assert naming.to_placeholder("まおはね", "まお") == f"{T}はね"          # 조사 は
    assert naming.to_placeholder("まおのこと", "まお") == f"{T}のこと"      # 조사 の
    assert naming.to_placeholder("まおちゃん", "まお") == f"{T}ちゃん"      # 경칭
    assert naming.to_placeholder("おはようまお。", "まお") == f"おはよう{T}。"      # 문장부호(문장 중간)
    assert naming.to_placeholder("今日はまおと話した", "まお") == f"今日は{T}と話した"  # 조사 と(문장 중간)
    assert naming.to_placeholder("健太が来た", "健太") == f"{T}が来た"      # 한자 이름 + 조사


def test_cjk_name_roundtrip():
    # 저장→렌더 라운드트립 보존(동일 이름). 조사·경칭·부호는 리터럴 유지.
    for text, nick in [("まおはね", "まお"), ("まおのこと", "まお"),
                       ("まおちゃん", "まお"), ("健太が来た", "健太"),
                       ("さくらんぼ", "さくら"),      # 미매칭도 라운드트립 동일
                       ("ゆうとが来た", "ゆう")]:      # 접두 과매칭(ゆう⊂ゆうと)도 동일이름이면 무손상
        assert naming.render(naming.to_placeholder(text, nick), nick) == text


def test_cjk_regex_excludes_hangul():
    # 하이픈 트랩 방지 회귀 — _CJK_RE가 한글을 절대 삼키지 않는다(리터럴 豈/U+F900 혼동 방어).
    for ch in "가힣나람각":
        assert not naming._CJK_RE.search(ch)
    # BMP 한자·가나 + 보조평면(Ext B 𠮷)·반각 가타카나(ｻ)·가타카나 음성확장(ㇰ)까지 커버.
    for ch in "愛張アさ\U00020bb7ｻㇰ":
        assert naming._CJK_RE.search(ch)


def test_cjk_supplementary_and_halfwidth_not_masked():
    # 1글자 CJK 이름(보조평면 한자·반각 가나)은 과치환 위험 커 마스킹 스킵(SOMA-365).
    assert naming.to_placeholder("\U00020bb7野で話した", "\U00020bb7") == "\U00020bb7野で話した"
    assert naming.to_placeholder("ｻﾄｼ", "ｻ") == "ｻﾄｼ"
