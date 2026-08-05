"""요약 출력 예산 — 추론 모델이 예산을 다 써서 빈 답이 나오지 않게 한다.

2026-08-05 dev 실측 사고다. `SUMMARY_MAX_OUTPUT_TOKENS=400`에서 원문 74건(4,050자)을 요약하면
**응답 텍스트가 0자**로 온다. 오류가 아니라 빈 문자열이라 예외로도 안 잡히고, mask_summary가
"비었다"로 거부해 재시도 3회가 전부 같은 이유로 실패한 뒤 잡이 dead가 된다.

즉 **대화가 길어진 사용자만 checkpoint가 영영 안 생긴다.** 짧은 대화에서는 재현되지 않아
테스트로도 안 잡혔다.
"""
from __future__ import annotations

import pytest

from app.services import checkpoint


def test_budget_leaves_room_for_reasoning_plus_answer():
    """실측에서 400은 실패하고 1000부터 성공했다. 하한을 코드로 고정한다."""
    assert checkpoint.SUMMARY_MAX_OUTPUT_TOKENS >= 1_000


def test_budget_is_much_larger_than_the_stored_answer():
    """저장 길이는 SUMMARY_MAX_CHARS가 따로 자른다 — 예산은 추론 자리까지 포함해야 한다.

    한국어는 대략 문자당 1토큰 미만이므로 답변만 보면 1200자 ≈ 800토큰이다. 예산이 그와
    비슷하면 추론 자리가 없다.
    """
    answer_tokens = checkpoint.SUMMARY_MAX_CHARS * 0.7
    assert checkpoint.SUMMARY_MAX_OUTPUT_TOKENS > answer_tokens * 1.5


def test_empty_provider_response_is_named_differently_from_masked_empty():
    """두 원인을 같은 메시지로 묶으면 예산 부족을 마스킹 문제로 오진한다."""
    with pytest.raises(checkpoint.CheckpointError) as empty:
        checkpoint.mask_summary("", None)
    assert "예산" in str(empty.value)

    with pytest.raises(checkpoint.CheckpointError) as masked:
        # 살균이 전부 지우는 입력 — provider는 응답을 줬다.
        checkpoint.mask_summary("​​", None)
    assert "예산" not in str(masked.value)


def test_whitespace_only_response_counts_as_empty():
    """공백만 온 것도 provider가 답을 못 준 것이다."""
    with pytest.raises(checkpoint.CheckpointError):
        checkpoint.mask_summary("   \n  ", None)
