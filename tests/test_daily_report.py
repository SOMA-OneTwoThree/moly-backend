from datetime import datetime, timedelta, timezone

from worker import daily_report as report

NOW = datetime(2026, 10, 20, 4, tzinfo=timezone.utc)


def fixture():
    data = {"activity": dict(signups=3, prior_signups=1, dau=12, prior_dau=10, mau=70),
            "subscriptions": dict(total=8, paid=5, trial=3, started=2, converted=1, ended=0),
            "diaries": dict(expected=4, completed=4, deleted=0)}
    live = dict(notes=[], worker="정기 실행: 정상 · 마지막 완료 12:45", retention="데이터 정리: 4/4 완료")
    return data, live


def test_totals_are_bounded_deduplicated_and_keep_only_counts():
    state = report.add_totals(None, NOW-timedelta(days=10), {"diaries": 2})
    state = report.add_totals(state, NOW, {"diaries": 3, "push_sent": 8, "user_id": "private"})
    assert list(state["days"]) == ["2026-10-20"]
    assert state["days"]["2026-10-20"]["diaries"] == 3
    assert "user_id" not in str(state)
    assert report.add_totals(state, NOW, {"diaries": 3}) == state
    assert report.add_totals(state, NOW-timedelta(seconds=1), {}) == state


def test_cost_pending_is_separate_from_service_status():
    data, live = fixture()
    message = report.render(NOW, data, live, None, dict(calls=3, priced=1, cost=50000, unknown=2), 1)
    assert "🟢 서비스 상태 정상" in message
    assert "비용 확인 중" in message and "일부 관측 불가" not in message
    assert "전일 보고보다 +1명" in message
    assert "푸시 알림: 일일 집계 중" in message


def test_missing_diary_is_actionable_without_tick_error():
    data, live = fixture()
    data["diaries"]["completed"] = 2
    message = report.render(NOW, data, live, None, None, None)
    assert "🟡 확인할 사항 있음" in message and "누락 2건" in message


def test_missing_data_is_not_rendered_as_zero_or_zero_dollar():
    data, live = fixture()
    data["activity"] = data["diaries"] = None
    message = report.render(NOW, data, live, None, dict(calls=2, priced=0), None)
    assert "이용 현황: 집계 확인 필요" in message
    assert "DAU: 0" not in message and "$0" not in message


def test_deleted_diary_is_excluded_from_generation_shortfall():
    data, live = fixture()
    data["diaries"].update(completed=3, deleted=1)
    message = report.render(NOW, data, live, None, None, None)
    assert "누락 0건 / 삭제 제외 1건" in message and "🟢" in message
