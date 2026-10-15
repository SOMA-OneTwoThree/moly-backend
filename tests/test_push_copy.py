"""서버 푸시 문구 무결성 — 언어 누락(SOMA-361 재발 방지), 확정 개수, 말투 규칙, 변수 채우기."""
from __future__ import annotations

import re

from app.services import push_copy

_LANGS = {"ko", "en", "ja"}
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿❤\U0001F90D]")


def test_every_copy_has_every_language():
    """언어 버킷 누락 시 i18n.pick이 en으로 조용히 폴백해 관측이 안 된다 — 구조로 막는다."""
    for table in (push_copy._CHAT, push_copy._ROUTINE):
        for variant, by_lang in table.items():
            assert set(by_lang) == _LANGS, f"{variant}: 언어 버킷 누락"
    assert set(push_copy._DIARY) == _LANGS


# 갈래별 확정 개수(2026-10-14 사용자가 줄 단위로 확정 — 삭제 반영 후 개수 고정).
_POOL_SIZES = {
    push_copy.MORE_CHAT: 6,
    push_copy.FIRST_TOUCH: 3,
    push_copy.DEFAULT_RECENT: 5,
    push_copy.DEFAULT_MISSING: 7,
    push_copy.DEFAULT_LONG: 3,
}


def test_pool_sizes_match_confirmed_counts():
    assert set(push_copy.CATEGORIES) == set(_POOL_SIZES)
    for c in push_copy.CATEGORIES:
        assert len(push_copy._POOLS[c]) == _POOL_SIZES[c], c


def test_every_chat_variant_belongs_to_one_pool_or_is_fallback():
    pooled = [v for c in push_copy.CATEGORIES for v in push_copy._POOLS[c]]
    assert len(pooled) == len(set(pooled))
    assert set(pooled) | {push_copy.FALLBACK} == set(push_copy._CHAT)


def test_confirmed_ko_lines_are_unchanged():
    """사용자 확정 문구 표본 — 손대면 안 되는 줄이 바뀌면 바로 알 수 있게."""
    assert push_copy.chat_body("c.missing.2", "ko") == "며칠 못 봤네 그동안 별일 없었어?"
    assert push_copy.chat_body("c.long.3", "ko") == "오늘 하루는 어땠어? 오랜만에 잠깐 얘기할까?"
    assert push_copy.diary("ko") == ("BeCappy", "캐피의 새 일기가 도착했어요 📖")


def test_chat_voice_rules():
    """캐피 말: 쉼표·이모지 없음(ko·en), ko는 마침표도 없음, 3인칭 자칭 없음, '수다' 없음."""
    for variant, by_lang in push_copy._CHAT.items():
        for lang in ("ko", "en"):
            assert "," not in by_lang[lang], f"{variant}/{lang}: 쉼표"
        for lang, body in by_lang.items():
            assert body and not _EMOJI.search(body), f"{variant}/{lang}: 이모지"
        ko = by_lang["ko"]
        assert "." not in ko, f"{variant}: 마침표"
        assert "캐피가" not in ko and "캐피는" not in ko and "수다" not in ko, variant
        assert not any("가" <= ch <= "힣" for ch in by_lang["ja"]), f"{variant}: ja에 한글"


def test_titles():
    for variant in push_copy._CHAT:
        for lang in _LANGS:
            assert push_copy.chat(variant, lang)[0] == "BeCappy"
    for lang in _LANGS:
        assert push_copy.diary(lang)[0] == "BeCappy"


def test_routine_copy_one_emoji_at_title_end_and_values_filled():
    for variant in push_copy._ROUTINE:
        for lang in _LANGS:
            title, body = push_copy.routine(variant, lang, n=8, lost=7)
            assert "{" not in title + body, f"{variant}/{lang}: 변수 미채움"
            bare = title.replace("\ufe0f", "")  # ❤️의 이모지 표시 문자
            assert len(_EMOJI.findall(bare)) == 1 and _EMOJI.search(bare[-1]), f"{variant}/{lang}: 제목 이모지"
    assert push_copy.routine("rr", "ko", n=8)[0] == "오늘 안 하면 8일 스트릭이 깨져요 🚨"
    assert push_copy.routine("rm.break", "ko", lost=7)[0] == "아쉽게도 7일 스트릭이 깨졌어요 😢"


def test_weekday_titles_follow_isoweekday():
    assert len(push_copy.WEEKDAYS) == 7
    assert push_copy.routine(push_copy.WEEKDAYS[0], "ko")[0] == "한 주의 시작, 월요일이에요 ❤️"
    assert push_copy.routine(push_copy.WEEKDAYS[6], "ko")[0] == "한 주를 마무리하는 일요일이에요 🤍"


def test_pick_stays_in_pool():
    for c in push_copy.CATEGORIES:
        assert {push_copy.pick(c) for _ in range(60)} <= set(push_copy._POOLS[c])


def test_unsupported_language_falls_back_to_en():
    assert push_copy.chat("c.more.1", "es") == ("BeCappy", push_copy._CHAT["c.more.1"]["en"])
    assert push_copy.chat_body("rr", "ko") is None  # 대화 문구가 아니면 첫마디로 잇지 않는다
