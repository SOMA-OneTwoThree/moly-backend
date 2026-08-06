"""서버 문구 다국어 — 저장된 BCP 47 언어 태그 → 콘텐츠 언어 버킷(SOMA-346).

콘텐츠는 ko·en·ja 지원(그 외 언어는 en 폴백). BCP 47 태그(ko-KR·en-US·zh-Hant-TW 등, SOMA-344)를
base 언어로 정규화해 버킷을 고른다. 이전엔 코드 곳곳이 `== "ko"` 정확일치라 `ko-KR`이 영어로 새는
버그가 있었다(SOMA-344 부작용) — 이 모듈이 단일 소스로 그걸 막는다.
"""
from __future__ import annotations

import logging
from typing import TypeVar

_log = logging.getLogger("moly-backend")

# 콘텐츠가 실제로 존재하는 언어(그 외는 폴백). 새 언어 카피가 준비되면 여기에 추가.
SUPPORTED = frozenset(("ko", "en", "ja"))
FALLBACK = "en"  # 지원 밖 언어(zh 등)에 적용할 폴백 — ko 앱이지만 미지원은 영어가 무난.
_DEFAULT = "en"  # 언어 미설정(None) = 영어. 해외 출시 기준으로 '모르는 언어'는 영어가 맞다.
# (DB `profiles.language`도 NOT NULL DEFAULT 'en'이고 트리거가 ko·en·ja로 좁힌다 —
#  20260806_normalize_profile_language.sql. 여기는 profile을 못 읽은 경우의 대비다.)

_V = TypeVar("_V")


def resolve(language: str | None) -> str:
    """BCP 47 태그 → 콘텐츠 언어 버킷(ko|en|ja). 미설정=ko, 지원 밖=en 폴백.

    예: None→ko, "ko-KR"→ko, "en-US"→en, "ja-JP"→ja, "zh-Hant-TW"→en(폴백).
    """
    base = (language or _DEFAULT).split("-", 1)[0].lower()
    if base in SUPPORTED:
        return base
    _log.info("i18n: 미지원 언어 %r → %s 폴백", language, FALLBACK)  # 번역 누락 관측
    return FALLBACK


def is_korean(language: str | None) -> bool:
    """콘텐츠 기준 한국어 여부. `resolve(x) == 'ko'`의 가독 별칭(문자/말투 게이팅용)."""
    return resolve(language) == "ko"


_missing_i18n_seen: set[tuple[str, str, str]] = set()


def localized_name(
    name_i18n: dict | None, language: str | None, default: str, *, kind: str = "catalog", key: str = ""
) -> str:
    """DB 카탈로그 다국어 이름 — resolve(lang)→en→ko 순으로 값이 있는 첫 키, 없으면 default(원문).

    pick()과 달리 유저 편집·부분 키가 가능한 DB 컬럼용이라 빈 문자열은 '누락'으로 보고 다음
    폴백으로 넘어간다(빈 이름 노출·min_length 위반 방지). NULL·부분키 모두 안전(SOMA-346).
    번역 누락은 (kind,key,lang) 단위 1회만 로그로 관측한다(스팸 방지, AC 운영 확인용).
    """
    resolved = resolve(language)
    # JSONB에 object가 아닌 scalar(문자열·숫자)가 들어와도 .get 500이 나지 않게 방어(DB CHECK와 이중).
    loc = name_i18n if isinstance(name_i18n, dict) else {}
    val = loc.get(resolved) or loc.get(FALLBACK) or loc.get(_DEFAULT)
    if val:
        return val
    if resolved != _DEFAULT:  # ko는 원문이 곧 정답이라 누락 아님
        sig = (kind, key, resolved)
        if sig not in _missing_i18n_seen:
            _missing_i18n_seen.add(sig)
            _log.info("i18n 카탈로그 번역 누락: %s %r → %s (원문 폴백)", kind, key, resolved)
    return default


def pick(table: dict[str, _V], language: str | None) -> _V:
    """{"ko": ..., "en": ..., "ja": ...} 표에서 언어에 맞는 값.

    버킷 키가 없으면 en(FALLBACK) → ko(원문) 순 폴백. 정확일치를 우선하므로 빈 문자열·빈
    리스트 같은 falsey 값도 키가 존재하면 그대로 반환한다(SOMA-361: ja 키 누락 시 한국어로
    뒤집히던 문제 방지 — 미지원 버킷은 영어가 무난).
    """
    resolved = resolve(language)
    if resolved in table:
        return table[resolved]
    if FALLBACK in table:
        return table[FALLBACK]
    return table["ko"]
