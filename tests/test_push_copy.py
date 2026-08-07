"""저녁 푸시 문구 풀 무결성 — ja/en 키 누락(SOMA-361 재발 방지), 빈 풀, 랜덤 선택 계약."""
from __future__ import annotations

from app.services import push_copy

_LANGS = {"ko", "en", "ja"}
_TITLES = {"ko": "캐피", "en": "Cappy", "ja": "キャピー"}


def test_every_category_has_every_language():
    """언어 버킷 누락 시 i18n.pick이 en으로 조용히 폴백해 관측이 안 된다 — 구조로 막는다."""
    for c in push_copy.CATEGORIES:
        assert set(push_copy._POOLS[c]) == _LANGS, f"{c}: 언어 버킷 누락"


# 카테고리별 확정 개수(2026-08-08 사용자 최종 검수 — 삭제 반영 후 개수 고정).
_POOL_SIZES = {
    push_copy.MORE_CHAT: 8,
    push_copy.DIARY_TEASER: 3,
    push_copy.FIRST_TOUCH: 3,
    push_copy.DEFAULT_RECENT: 7,
    push_copy.DEFAULT_MISSING: 7,
    push_copy.DEFAULT_LONG: 5,
}


def test_pool_sizes_match_confirmed_counts():
    for c in push_copy.CATEGORIES:
        expected = _POOL_SIZES[c]
        for lang, pool in push_copy._POOLS[c].items():
            assert len(pool) == expected, f"{c}/{lang}: 풀 크기 {len(pool)} != {expected}"
            for title, body in pool:
                assert title and body, f"{c}/{lang}: 빈 문구"


def test_no_third_person_self_reference_in_ko():
    """캐피는 1인칭(나) — '캐피가~' '캐피는~' 3인칭 자칭 금지(사용자 확정).

    자기소개("나는 캐피야")의 '캐피야'는 허용 — 이름을 말하는 것이지 자칭이 아니다.
    diary_teaser는 예외: 캐피 발화가 아니라 시스템 안내 보이스(존댓말)라서
    "캐피가 일기를 적어두었어요"가 컨셉이다(2026-08-07 사용자 확정).
    """
    for c in push_copy.CATEGORIES:
        if c == push_copy.DIARY_TEASER:
            continue
        for _, body in push_copy._POOLS[c]["ko"]:
            assert "캐피가" not in body and "캐피는" not in body, f"{c}: 3인칭 자칭 — {body!r}"


def test_no_commas_in_ko_and_en():
    """쉼표 금지는 캐피 말투의 핵심(CAPI_PERSONA [부호]) — 푸시 문구도 따른다."""
    for c in push_copy.CATEGORIES:
        for lang in ("ko", "en"):
            for _, body in push_copy._POOLS[c][lang]:
                assert "," not in body, f"{c}/{lang}: 쉼표 — {body!r}"


def test_no_duplicate_bodies_within_pool():
    """같은 풀 안의 중복은 다양화를 조용히 줄인다."""
    for c in push_copy.CATEGORIES:
        for lang, pool in push_copy._POOLS[c].items():
            bodies = [b for _, b in pool]
            assert len(bodies) == len(set(bodies)), f"{c}/{lang}: 본문 중복"


def test_titles_are_fixed_per_language():
    """캐릭터명 표면 규칙(SOMA-361): ko 캐피 / en Cappy / ja キャピー."""
    for c in push_copy.CATEGORIES:
        for lang, pool in push_copy._POOLS[c].items():
            for title, _ in pool:
                assert title == _TITLES[lang], f"{c}/{lang}: 제목 {title!r}"


def test_ja_bodies_have_no_hangul():
    for c in push_copy.CATEGORIES:
        for _, body in push_copy._POOLS[c]["ja"]:
            assert not any("가" <= ch <= "힣" for ch in body), f"{c}: ja 본문에 한글"


def test_pick_stays_in_language_pool():
    """60회 샘플링이 해당 언어 풀의 부분집합(test_greetings 관례 — seed 주입 안 함)."""
    for c in push_copy.CATEGORIES:
        picked = {push_copy.pick(c, "ko") for _ in range(60)}
        assert picked <= set(push_copy._POOLS[c]["ko"])


def test_unsupported_language_falls_back_to_en():
    for c in push_copy.CATEGORIES:
        picked = {push_copy.pick(c, "es") for _ in range(30)}
        assert picked <= set(push_copy._POOLS[c]["en"])
