"""서버 문구 언어 버킷 리졸버(SOMA-346·361) — BCP 47 태그 → ko|en|ja."""
from app.services import i18n


def test_resolve_buckets():
    # 미설정 = 영어. 해외 출시 기준으로 '모르는 언어'는 영어가 맞다. 운영 DB의
    # profiles.language는 NOT NULL DEFAULT 'en'이라 이 경로는 profile을 못 읽은 경우뿐이다.
    assert i18n.resolve(None) == "en"
    assert i18n.resolve("") == "en"
    assert i18n.resolve("ko") == "ko"
    assert i18n.resolve("ko-KR") == "ko"       # BCP47 지역 태그 → base 언어
    assert i18n.resolve("en") == "en"
    assert i18n.resolve("en-US") == "en"
    assert i18n.resolve("ja") == "ja"          # SOMA-361: 일본어 정식 지원
    assert i18n.resolve("ja-JP") == "ja"       # 지역 태그도 base 정규화
    assert i18n.resolve("zh-Hant-TW") == "en"  # 미지원 언어 → en 폴백


def test_is_korean():
    assert i18n.is_korean("ko") and i18n.is_korean("ko-KR")
    assert not (i18n.is_korean("en") or i18n.is_korean("ja") or i18n.is_korean("zh-Hant-TW"))
    assert not i18n.is_korean(None), "미설정은 이제 영어다 — 한국어로 새면 비한국어 유저가 한국어를 본다"


def test_pick():
    t = {"ko": "가", "en": "A", "ja": "あ"}
    assert i18n.pick(t, "ko") == "가"
    assert i18n.pick(t, "ko-KR") == "가"
    assert i18n.pick(t, "en-US") == "A"
    assert i18n.pick(t, "ja") == "あ"           # 정식 지원 버킷
    assert i18n.pick(t, "zh") == "A"            # 미지원 → en 폴백


def test_pick_fallback_chain():
    # SOMA-361: ja 키가 없으면 en(FALLBACK)으로, en도 없으면 ko 원문으로.
    # (ja를 SUPPORTED에 넣은 뒤 ja 키 누락 시 한국어로 뒤집히지 않게 하는 게 핵심.)
    assert i18n.pick({"ko": "가", "en": "A"}, "ja") == "A"
    assert i18n.pick({"ko": "가"}, "ja") == "가"
    assert i18n.pick({"ko": "가"}, "en") == "가"
    # 정확일치 우선 — falsey 값도 키가 존재하면 그대로 반환한다.
    assert i18n.pick({"ko": "가", "en": ""}, "en") == ""


def test_content_language_is_only_three_buckets():
    """콘텐츠 언어는 ko·en·ja 셋뿐이다. 어떤 값이 들어와도 그 밖으로 나가면 안 된다.

    DB `profiles.language`도 트리거로 같은 셋으로 좁힌다
    (db/migrations/20260806_normalize_profile_language.sql). 여기는 코드 쪽 대비다.
    """
    probes = [
        None, "", "  ", "ko", "ko-KR", "en", "en-US", "en-GB", "ja", "ja-JP",
        "zh", "zh-Hant-TW", "th", "es-ES", "fr-FR", "vi", "de", "pt-BR", "it-IT",
        "KO", "Ko-kR", "x-invalid", "12345",
    ]
    for tag in probes:
        assert i18n.resolve(tag) in {"ko", "en", "ja"}, f"{tag!r}가 셋 밖으로 나갔다"
