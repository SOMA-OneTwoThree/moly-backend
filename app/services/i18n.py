"""서버 문구 다국어 — 저장된 BCP 47 언어 태그 → 콘텐츠 언어 버킷(SOMA-346).

콘텐츠는 ko·en만 존재(그 외 언어는 en 폴백). BCP 47 태그(ko-KR·en-US·zh-Hant-TW 등, SOMA-344)를
base 언어로 정규화해 버킷을 고른다. 이전엔 코드 곳곳이 `== "ko"` 정확일치라 `ko-KR`이 영어로 새는
버그가 있었다(SOMA-344 부작용) — 이 모듈이 단일 소스로 그걸 막는다.
"""
from __future__ import annotations

import logging
from typing import TypeVar

_log = logging.getLogger("moly-backend")

# 콘텐츠가 실제로 존재하는 언어(그 외는 폴백). 새 언어 카피가 준비되면 여기에 추가.
SUPPORTED = ("ko", "en")
FALLBACK = "en"  # 지원 밖 언어(zh·ja 등)에 적용할 폴백 — ko 앱이지만 미지원은 영어가 무난.
_DEFAULT = "ko"  # 언어 미설정(None) = 한국어(기본 프로필).

_V = TypeVar("_V")


def resolve(language: str | None) -> str:
    """BCP 47 태그 → 콘텐츠 언어 버킷(ko|en). 미설정=ko, 지원 밖=en 폴백.

    예: None→ko, "ko"→ko, "ko-KR"→ko, "en-US"→en, "zh-Hant-TW"→en(폴백).
    """
    base = (language or _DEFAULT).split("-", 1)[0].lower()
    if base in SUPPORTED:
        return base
    _log.info("i18n: 미지원 언어 %r → %s 폴백", language, FALLBACK)  # 번역 누락 관측
    return FALLBACK


def is_korean(language: str | None) -> bool:
    """콘텐츠 기준 한국어 여부. `resolve(x) == 'ko'`의 가독 별칭(문자/말투 게이팅용)."""
    return resolve(language) == "ko"


def pick(table: dict[str, _V], language: str | None) -> _V:
    """{"ko": ..., "en": ...} 표에서 언어에 맞는 값. 버킷에 없으면 ko(원문) 폴백."""
    return table.get(resolve(language), table["ko"])
