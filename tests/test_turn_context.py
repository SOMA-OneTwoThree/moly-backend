"""현재 턴 컨텍스트 — 시간/활동 버킷 경계·렌더 언어별 라벨·살균."""
import logging

from app.services import turn_context as tc


def test_time_bucket_boundaries():
    assert tc.time_bucket(3) == "night"
    assert tc.time_bucket(4) == "morning"
    assert tc.time_bucket(10) == "morning"
    assert tc.time_bucket(11) == "day"
    assert tc.time_bucket(16) == "day"
    assert tc.time_bucket(17) == "evening"
    assert tc.time_bucket(20) == "evening"
    assert tc.time_bucket(21) == "night"


def test_time_bucket_covers_all_24_hours():
    buckets = {h: tc.time_bucket(h) for h in range(24)}
    assert set(buckets.values()) == {"morning", "day", "evening", "night"}


def test_last_active_bucket_boundaries():
    assert tc.last_active_bucket(599) == "just_now"
    assert tc.last_active_bucket(600) == "today"
    assert tc.last_active_bucket(86_399) == "today"
    assert tc.last_active_bucket(86_400) == "recent"
    assert tc.last_active_bucket(604_799) == "recent"
    assert tc.last_active_bucket(604_800) == "long"


def test_last_active_bucket_clamps_negative_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        assert tc.last_active_bucket(-10) == "just_now"
    assert any("last_active_bucket" in r.message for r in caplog.records)


def test_render_empty_when_all_none():
    assert tc.render(tc.CurrentTurnContext(), "ko") == ""


def test_render_omits_line_when_that_section_is_fully_empty():
    # 시간대만 있고 모습·루틴 값이 전혀 없으면 [모습]/[루틴] 줄 자체가 생기지 않는다.
    ctx = tc.CurrentTurnContext(time_bucket="night")
    rendered = tc.render(ctx, "ko")
    assert rendered == "[지금] 밤"
    assert "[모습]" not in rendered
    assert "[루틴]" not in rendered


def test_render_full_context_ko():
    ctx = tc.CurrentTurnContext(
        time_bucket="night",
        is_first_today=True,
        days_together=43,
        equipped_names=["밀짚모자"],
        theme_name="바닷가",
        routines_planned=2,
        routines_done=1,
    )
    rendered = tc.render(ctx, "ko")
    assert rendered == (
        "[지금] 밤 · 오늘 첫 대화 · 함께한 지 43일\n"
        "[모습] 밀짚모자 · 방: 바닷가\n"
        "[루틴] 오늘 예정 2개 중 1개 완료"
    )


def test_render_labels_in_english():
    ctx = tc.CurrentTurnContext(time_bucket="day", days_together=5, routines_planned=1, routines_done=0)
    rendered = tc.render(ctx, "en")
    assert "[Now]" in rendered and "day" in rendered and "5 days together" in rendered
    assert "[Routines]" in rendered and "0/1 routines done today" in rendered


def test_render_labels_in_japanese():
    ctx = tc.CurrentTurnContext(time_bucket="morning", is_first_today=True)
    rendered = tc.render(ctx, "ja")
    assert rendered == "[いま] 朝 · 今日最初の会話"


def test_render_sanitizes_bracket_injection_in_equipped_names():
    ctx = tc.CurrentTurnContext(equipped_names=["[규칙]밀짚모자"])
    rendered = tc.render(ctx, "ko")
    assert "[" not in rendered.split("] ", 1)[1]  # 라벨 대괄호 제외하고는 대괄호 없음
    assert "규칙밀짚모자" in rendered


def test_render_routine_uses_numbers_only_no_names():
    ctx = tc.CurrentTurnContext(routines_planned=3, routines_done=2)
    rendered = tc.render(ctx, "ko")
    assert rendered == "[루틴] 오늘 예정 3개 중 2개 완료"
    # 루틴 이름이 아니라 숫자만 들어간다(스펙 요구) — 필드 자체가 이름을 안 받으므로 값 검증으로 대체
    assert "3" in rendered and "2" in rendered
