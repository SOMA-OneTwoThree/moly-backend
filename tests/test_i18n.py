"""서버 문구 언어 버킷 리졸버(SOMA-346) — BCP 47 태그 → ko|en."""
from app.services import i18n


def test_resolve_buckets():
    assert i18n.resolve(None) == "ko"          # 미설정 = 한국어(기본 프로필)
    assert i18n.resolve("ko") == "ko"
    assert i18n.resolve("ko-KR") == "ko"       # BCP47 지역 태그 → base 언어
    assert i18n.resolve("en") == "en"
    assert i18n.resolve("en-US") == "en"
    assert i18n.resolve("zh-Hant-TW") == "en"  # 미지원 언어 → en 폴백
    assert i18n.resolve("ja") == "en"


def test_is_korean():
    assert i18n.is_korean(None) and i18n.is_korean("ko") and i18n.is_korean("ko-KR")
    assert not (i18n.is_korean("en") or i18n.is_korean("en-US") or i18n.is_korean("zh-Hant-TW"))


def test_pick():
    t = {"ko": "가", "en": "A"}
    assert i18n.pick(t, "ko") == "가"
    assert i18n.pick(t, "ko-KR") == "가"
    assert i18n.pick(t, "en-US") == "A"
    assert i18n.pick(t, "ja") == "A"               # 미지원 → en 버킷
    assert i18n.pick({"ko": "가"}, "en") == "가"   # en 없으면 ko 원문 폴백
