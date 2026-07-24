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
