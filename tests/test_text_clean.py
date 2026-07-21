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
