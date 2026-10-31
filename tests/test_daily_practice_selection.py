"""선택 함수 단위 테스트 — DB 없이 입력과 seed만으로 결과를 고정한다."""
import random
import uuid
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.services import daily_practice_selection as selection
from app.services.daily_practice_selection import Shown, choose, slot_of

TODAY = date(2026, 11, 1)
ALL_SLOTS = ("morning", "day", "evening", "night")


def action(category="rest", slots=ALL_SLOTS, n=None):
    return SimpleNamespace(id=uuid.UUID(int=n) if n else uuid.uuid4(), category=category, slots=slots)


def one_per_category():
    return [action(category, n=i + 1) for i, category in enumerate(selection.CATEGORIES)]


def shown(item, status="offered", days_ago=1):
    return Shown(item.id, item.category, status, TODAY - timedelta(days=days_ago))


def pick(actions, *, slot="day", history=(), focus=(), moods=(), seed=1):
    return choose(actions, slot=slot, today=TODAY, history=history, focus=focus, moods=moods,
                  rng=random.Random(seed))


def win_rate(actions, category, trials=200, **kwargs):
    wins = sum(pick(actions, seed=seed, **kwargs).action.category == category for seed in range(trials))
    return wins / trials


@pytest.mark.parametrize(("hour", "slot"), [
    (5, "morning"), (10, "morning"), (11, "day"), (16, "day"), (17, "evening"), (21, "evening"),
    (22, "night"), (23, "night"), (0, "night"), (4, "night"),
])
def test_slot_boundaries_follow_the_contract(hour, slot):
    assert slot_of(hour) == slot


def test_only_actions_that_fit_the_time_of_day_are_candidates():
    morning, night = action(slots=("morning",)), action(slots=("evening", "night"))
    assert all(pick([morning, night], slot="night", seed=s).action is night for s in range(30))


def test_nothing_fits_returns_none_and_asks_for_focus_categories():
    result = pick([action(slots=("morning",))], slot="night", focus=("calm", "mind"))
    assert result.action is None and result.short_categories == ("calm", "mind")


def test_empty_pool_without_focus_asks_for_every_category():
    result = pick([])
    assert result.action is None and result.short_categories == selection.CATEGORIES


def test_shown_within_30_days_is_excluded_and_older_is_back():
    recent, old = action(n=1), action(n=2)
    history = [shown(recent, days_ago=30), shown(old, days_ago=31)]
    assert all(pick([recent, old], history=history, seed=s).action is old for s in range(30))


def test_anything_offered_today_is_excluded_even_when_relaxed():
    today_item, other = action(n=1), action(n=2)
    history = [shown(today_item, days_ago=0), shown(other, days_ago=2)]
    result = pick([today_item, other], history=history)
    assert result.action is other


def test_no_fresh_action_relaxes_to_least_recently_shown_and_requests_top_up():
    actions = [action("mind", n=i) for i in range(1, 9)]
    history = [shown(item, days_ago=i + 1) for i, item in enumerate(actions)]
    oldest = {item.id for item in actions[-selection.LEAST_RECENT_KEEP:]}
    for seed in range(30):
        result = pick(actions, history=history, seed=seed)
        assert result.action.id in oldest
        assert result.short_categories == ("mind",)


def test_top_up_request_includes_the_first_focus_category_when_relaxed():
    item = action("mind")
    result = pick([item], history=[shown(item, days_ago=3)], focus=("calm", "rest"))
    assert result.short_categories == ("calm", "mind")


def test_no_top_up_when_a_fresh_action_exists():
    assert pick([action()]).short_categories == ()


def test_focus_first_category_is_strongest_for_a_new_user():
    actions = one_per_category()
    first = win_rate(actions, "calm", focus=("calm", "mind"))
    second = win_rate(actions, "mind", focus=("calm", "mind"))
    assert first > 0.95 and second < 0.05
    assert win_rate(actions, "mind", focus=("mind", "calm")) > 0.95


def test_focus_rank_scores_decrease():
    scores = selection.category_scores([], ("calm", "mind", "rest"), [], TODAY)
    assert scores["calm"] > scores["mind"] > scores["rest"] > scores["order"] == 0


def test_focus_fades_as_completions_and_skips_accumulate():
    assert selection.focus_strength(0) == selection.FOCUS_MAX
    assert selection.focus_strength(selection.FOCUS_FADE_EVENTS) == selection.FOCUS_MAX / 2
    assert selection.focus_strength(80) < selection.focus_strength(8) < selection.focus_strength(1)

    actions = one_per_category()
    filler = action("order")
    many = [shown(filler, status="completed", days_ago=20) for _ in range(30)]
    many += [shown(filler, status="skipped", days_ago=20) for _ in range(30)]
    fresh_user = win_rate(actions, "calm", focus=("calm",))
    veteran = win_rate(actions, "calm", focus=("calm",), history=many)
    assert veteran < fresh_user


def test_offered_rows_do_not_count_as_history_events():
    item = action("order")
    with_offers = selection.category_scores([shown(item) for _ in range(40)], ("calm",), [], TODAY)
    assert with_offers["calm"] == selection.FOCUS_MAX
    assert with_offers["order"] == 0


def test_completions_raise_and_skips_lower_a_category():
    done, skipped = action("mind"), action("order")
    scores = selection.category_scores(
        [shown(done, "completed"), shown(skipped, "skipped")], (), [], TODAY
    )
    assert scores["mind"] > 0 > scores["order"]
    assert scores["rest"] == 0


def test_recent_history_counts_more_than_old_history():
    item = action("mind")
    recent = selection.category_scores([shown(item, "completed", days_ago=1)], (), [], TODAY)
    old = selection.category_scores([shown(item, "completed", days_ago=50)], (), [], TODAY)
    assert recent["mind"] > old["mind"] > 0


def test_history_effect_is_clamped():
    item = action("mind")
    scores = selection.category_scores(
        [shown(item, "skipped", days_ago=1) for _ in range(50)], (), [], TODAY
    )
    assert scores["mind"] == -selection.HISTORY_CLAMP


def test_low_mood_leans_to_rest_calm_and_self_kindness():
    scores = selection.category_scores([], (), ["tired", "annoyed"], TODAY)
    assert scores["rest"] == scores["calm"] == scores["self_kindness"] == selection.MOOD_LEAN
    assert scores["body"] == scores["connect"] == scores["order"] == scores["mind"] == 0


@pytest.mark.parametrize("moods", [["content", "excited", "neutral"], [], ["custom-kind"]])
def test_other_moods_do_not_lean(moods):
    scores = selection.category_scores([], (), moods, TODAY)
    assert all(value == 0 for value in scores.values())


def test_mixed_mood_leans_in_proportion():
    scores = selection.category_scores([], (), ["tired", "content"], TODAY)
    assert scores["calm"] == selection.MOOD_LEAN / 2


def test_low_mood_changes_what_gets_picked():
    actions = one_per_category()
    lean = {"rest", "calm", "self_kindness"}
    plain = sum(pick(actions, seed=s).action.category in lean for s in range(200))
    low = sum(pick(actions, moods=["tired"], seed=s).action.category in lean for s in range(200))
    assert low == 200 > plain


def test_a_skip_avoids_that_category_for_the_rest_of_the_day():
    skipped, same_category, other = action("calm", n=1), action("calm", n=2), action("order", n=3)
    history = [shown(skipped, "skipped", days_ago=0)]
    for seed in range(30):
        result = pick([skipped, same_category, other], history=history, focus=("calm",), seed=seed)
        assert result.action is other
        assert result.short_categories == ()


def test_a_skip_from_yesterday_does_not_avoid_the_category():
    skipped, same_category = action("calm", n=1), action("calm", n=2)
    result = pick([same_category], history=[shown(skipped, "skipped", days_ago=1)])
    assert result.action is same_category


def test_avoiding_prefers_a_repeat_of_another_category_over_the_skipped_one():
    skipped, fresh_same, old_other = action("calm", n=1), action("calm", n=2), action("order", n=3)
    history = [shown(skipped, "skipped", days_ago=0), shown(old_other, days_ago=4)]
    result = pick([skipped, fresh_same, old_other], history=history)
    assert result.action is old_other
    assert result.short_categories == ("order",)


def test_avoiding_gives_way_when_only_that_category_is_left():
    skipped, fresh_same = action("calm", n=1), action("calm", n=2)
    result = pick([skipped, fresh_same], history=[shown(skipped, "skipped", days_ago=0)])
    assert result.action is fresh_same
    assert result.short_categories == ("calm",)


def test_same_seed_gives_the_same_action_regardless_of_input_order():
    actions = [action(category, n=i * 10 + j + 1) for i, category in enumerate(selection.CATEGORIES)
               for j in range(3)]
    first = pick(actions, seed=7).action
    assert pick(list(reversed(actions)), seed=7).action is first


def test_noise_varies_the_pick_between_seeds():
    actions = one_per_category()
    assert len({pick(actions, seed=s).action.category for s in range(100)}) > 1
