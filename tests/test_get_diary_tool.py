"""일기 조회 도구 — 같은 질문에 됐다 안 됐다 하지 않는다.

감사 실측: "저번에 너가 써준 일기 읽어주라"를 세 번 물었는데 T1 "안 보인다" / T2 정확히
조회 / T3 다시 "안 보인다". dev DB에는 처음부터 일기가 있었다.

원인은 도구가 **날짜를 안 주면 오늘로 처리**한 것이다. 오늘은 대개 일기가 없어 null이
돌아왔고, 모델이 날짜를 찍어 맞혀야 해서 확률적으로 흔들렸다.
"""
from __future__ import annotations

from app.services.agent.tools.get_diary import GetDiaryArgs, GetDiaryTool




# ── 날짜 없이 부르면 최신 일기 (감사 지적) ──────────────────

def test_omitting_date_is_documented_as_most_recent():
    """예전엔 '오늘'로 처리했다. 오늘은 대개 일기가 없어 null이 돌아왔고, 캐피가
    "일기가 안 보인다"고 답했다. 모델이 날짜를 찍어 맞혀야 해서 같은 질문에도 됐다
    안 됐다 했다(실측 3번 중 1번만 성공).
    """
    field = GetDiaryArgs.model_fields["date"]
    assert "most recent" in (field.description or "")


def test_query_does_not_pin_the_date_when_omitted():
    """날짜를 안 줬는데 오늘로 고정하면 최신 일기를 못 찾는다."""
    import inspect

    src = inspect.getsource(GetDiaryTool.run)
    assert "args.date is not None" in src, "날짜 유무를 구분하지 않는다"
    assert "or ctx.activity_date" not in src, "여전히 오늘로 대체한다"


def test_description_covers_indirect_diary_mentions():
    """'읽어주라'처럼 간접적으로 말해도 도구를 골라야 한다."""
    d = GetDiaryTool.description
    assert "read it aloud" in d or "in any way" in d


def test_out_of_range_check_only_applies_to_explicit_dates():
    """날짜를 안 준 경우까지 범위 검사에 걸리면 최신 조회가 막힌다."""
    import inspect

    src = inspect.getsource(GetDiaryTool.run)
    idx = src.index("date_out_of_range")
    assert "args.date is not None" in src[:idx]
