"""기억·현재상태의 프롬프트 렌더에 공통으로 쓰는 텍스트 살균.

장기기억 저장·검색은 `memory_repo`와 pgvector가 담당한다. 이 모듈에는 외부 저장소나
런타임 전환 경로가 없다.
"""
from __future__ import annotations

import unicodedata

_STRIP = dict.fromkeys(
    list(range(0x00, 0x20))
    + list(range(0x7F, 0xA0))
    + [
        0x200B,
        0x200C,
        0x200D,
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
        0xFEFF,
    ],
    None,
)
_BRACKETS = {ord(c): None for c in "<>[]＜＞［］〈〉【】〈〉"}


def sanitize_text(text: str) -> str:
    """NFKC 후 제어문자·bidi·대괄호를 제거해 프롬프트 섹션 위조를 막는다."""
    normalized = unicodedata.normalize("NFKC", text)
    return normalized.translate(_STRIP).translate(_BRACKETS).strip()


_sanitize = sanitize_text  # 현재 턴 컨텍스트의 기존 내부 import 호환
