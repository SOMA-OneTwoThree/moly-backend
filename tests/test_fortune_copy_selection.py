from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import inspect

import pytest

from app.services import fortune_copy_selection as selection

TODAY = date(2026, 9, 8)


def _semantic(overall: str = 'overall.d50.balance.default', decile: str = 'd50') -> dict:
    return {
        'schema_version': 3,
        'overall': {'score': 50, 'expression_route': overall},
        'categories': {
            key: {'score': int(decile[1:]), 'expression_route': f'category.{key}.{decile}.general'}
            for key in selection.CATEGORY_KEYS
        },
        'lucky_color_key': 'green',
    }


def _snapshot(semantic: dict, today: date = TODAY, previous: dict | None = None) -> dict:
    result = deepcopy(semantic)
    result['copy_selection'] = selection.select_variants(
        semantic, today=today, previous_semantic=previous
    )
    return result


def test_fixed_order_golden_is_stable_across_calls_and_cache_eviction():
    expected = (
        'v08', 'v09', 'v16', 'v03', 'v11', 'v17', 'v02', 'v13', 'v12', 'v19',
        'v10', 'v06', 'v05', 'v20', 'v14', 'v07', 'v04', 'v15', 'v01', 'v18',
    )
    assert selection.variant_order('overall.d50.balance.default') == expected
    selection.variant_order.cache_clear()
    assert selection.variant_order('overall.d50.balance.default') == expected
    # Presentation selection has no user, language, or request-clock input.
    assert set(inspect.signature(selection.select_variants).parameters) == {
        'semantic', 'today', 'previous_semantic'
    }


@pytest.mark.parametrize('route', sorted(selection.ALL_ROUTES))
def test_every_route_has_twenty_unique_variants_then_repeats_on_21st_advance(route):
    semantic = _semantic()
    slot = 'overall' if route.startswith('overall.') else route.split('.')[1]
    if slot == 'overall':
        semantic['overall']['expression_route'] = route
    else:
        semantic['categories'][slot]['expression_route'] = route
    snapshot = None
    seen = []
    for offset in range(21):
        snapshot = _snapshot(semantic, TODAY + timedelta(days=offset), snapshot)
        seen.append(snapshot['copy_selection']['selected'][slot])
    assert set(seen[:20]) == set(selection.VARIANT_IDS)
    assert len(set(seen[:20])) == 20
    assert seen[20] == seen[0]


def test_same_and_past_dates_reuse_both_position_and_date_high_water_mark():
    semantic = _semantic()
    first = _snapshot(semantic)
    second = _snapshot(semantic, TODAY + timedelta(days=1), first)
    for revisit in (TODAY + timedelta(days=1), TODAY, TODAY - timedelta(days=10)):
        reused = _snapshot(semantic, revisit, second)
        assert reused['copy_selection'] == second['copy_selection']
        # Returning to the high-water date must not consume another variant.
        resumed = _snapshot(semantic, TODAY + timedelta(days=1), reused)
        assert resumed['copy_selection'] == second['copy_selection']
    third = _snapshot(semantic, TODAY + timedelta(days=2), resumed)
    assert third['copy_selection']['routes']['overall.d50.balance.default']['position'] == 2


def test_skipping_calendar_dates_advances_only_once_per_new_snapshot():
    semantic = _semantic()
    first = _snapshot(semantic)
    later = _snapshot(semantic, TODAY + timedelta(days=40), first)
    assert later['copy_selection']['routes']['overall.d50.balance.default']['position'] == 1


def test_route_switch_and_return_preserve_each_cursor_independently():
    a = _semantic()
    b = _semantic('overall.d60.focus.default', 'd60')
    first = _snapshot(a)
    switched = _snapshot(b, TODAY + timedelta(days=1), first)
    assert len(switched['copy_selection']['routes']) == 10
    for route, cursor in first['copy_selection']['routes'].items():
        assert switched['copy_selection']['routes'][route] == cursor
    returned = _snapshot(a, TODAY + timedelta(days=2), switched)
    for route in first['copy_selection']['routes']:
        assert returned['copy_selection']['routes'][route]['position'] == 1
    for route in set(switched['copy_selection']['routes']) - set(first['copy_selection']['routes']):
        assert returned['copy_selection']['routes'][route]['position'] == 0
    same_day_b = _snapshot(b, TODAY + timedelta(days=2), returned)
    same_day_a = _snapshot(a, TODAY + timedelta(days=2), same_day_b)
    assert same_day_a['copy_selection']['selected'] == returned['copy_selection']['selected']


def test_inputs_and_returned_state_do_not_alias_each_other():
    semantic = _semantic()
    previous = _snapshot(semantic)
    original_semantic, original_previous = deepcopy(semantic), deepcopy(previous)
    output = selection.select_variants(semantic, today=TODAY + timedelta(days=1), previous_semantic=previous)
    assert semantic == original_semantic
    assert previous == original_previous
    output['routes']['overall.d50.balance.default']['position'] = 19
    output['selected']['overall'] = 'v20'
    assert previous == original_previous
    assert semantic == original_semantic


def test_all_routes_can_be_retained_but_no_121st_route_is_allowed():
    previous = _snapshot(_semantic())
    previous['copy_selection']['routes'] = {
        route: {'day': TODAY.isoformat(), 'position': 0} for route in selection.ALL_ROUTES
    }
    assert len(previous['copy_selection']['routes']) == 120
    result = selection.select_variants(_semantic(), today=TODAY + timedelta(days=1), previous_semantic=previous)
    assert len(result['routes']) == 120
    previous['copy_selection']['routes']['overall.d100.balance.default'] = {
        'day': TODAY.isoformat(), 'position': 0
    }
    with pytest.raises(selection.FortuneSelectionError):
        selection.select_variants(_semantic(), today=TODAY, previous_semantic=previous)


@pytest.mark.parametrize('invalid', [None, {}, [], {'version': 'unknown'}])
def test_existing_invalid_state_is_never_treated_as_legacy(invalid):
    previous = _semantic()
    previous['copy_selection'] = invalid
    with pytest.raises(selection.FortuneSelectionError):
        selection.select_variants(_semantic(), today=TODAY, previous_semantic=previous)


def test_absent_state_is_supported_for_legacy_snapshot():
    without_previous = selection.select_variants(_semantic(), today=TODAY)
    legacy = selection.select_variants(_semantic(), today=TODAY, previous_semantic=_semantic())
    assert legacy == without_previous


@pytest.mark.parametrize('position', [-1, 20, True, False, 1.0, '1', None])
def test_rejects_invalid_cursor_position(position):
    previous = _snapshot(_semantic())
    previous['copy_selection']['routes']['overall.d50.balance.default']['position'] = position
    with pytest.raises(selection.FortuneSelectionError):
        selection.select_variants(_semantic(), today=TODAY, previous_semantic=previous)


@pytest.mark.parametrize('day', ['2026-02-30', '20260908', '2026-W37-2', '2026-9-8', '', None, TODAY])
def test_rejects_invalid_or_noncanonical_cursor_date(day):
    previous = _snapshot(_semantic())
    previous['copy_selection']['routes']['overall.d50.balance.default']['day'] = day
    with pytest.raises(selection.FortuneSelectionError):
        selection.select_variants(_semantic(), today=TODAY, previous_semantic=previous)


@pytest.mark.parametrize('mutation', ['version', 'extra_key', 'missing_selected', 'bad_selected', 'unknown_route', 'cursor_extra'])
def test_rejects_invalid_state_shapes_and_identifiers(mutation):
    previous = _snapshot(_semantic())
    state = previous['copy_selection']
    if mutation == 'version':
        state['version'] = 'fortune-selection.v999'
    elif mutation == 'extra_key':
        state['events'] = []
    elif mutation == 'missing_selected':
        del state['selected']['love']
    elif mutation == 'bad_selected':
        state['selected']['overall'] = 'v21'
    elif mutation == 'unknown_route':
        state['routes']['overall.d50.unknown.default'] = {'day': TODAY.isoformat(), 'position': 0}
    else:
        state['routes']['overall.d50.balance.default']['count'] = 1
    with pytest.raises(selection.FortuneSelectionError):
        selection.select_variants(_semantic(), today=TODAY, previous_semantic=previous)


@pytest.mark.parametrize('mutation', ['missing_current_cursor', 'mismatched_selected'])
def test_rejects_state_inconsistent_with_previously_selected_snapshot(mutation):
    previous = _snapshot(_semantic())
    state = previous['copy_selection']
    if mutation == 'missing_current_cursor':
        del state['routes']['overall.d50.balance.default']
    else:
        actual = state['selected']['overall']
        state['selected']['overall'] = next(v for v in selection.VARIANT_IDS if v != actual)
    with pytest.raises(selection.FortuneSelectionError):
        selection.select_variants(_semantic(), today=TODAY, previous_semantic=previous)


@pytest.mark.parametrize('route', ['overall.d100.start.default', 'overall.d50.start.v01', 'category.health.d50.general'])
def test_unknown_current_route_is_rejected(route):
    semantic = _semantic(route)
    with pytest.raises(selection.FortuneSelectionError):
        selection.select_variants(semantic, today=TODAY)


@pytest.mark.parametrize('slot,wrong_route', [
    ('overall', 'category.love.d50.general'),
    ('love', 'overall.d50.balance.default'),
    ('love', 'category.money.d50.general'),
])
def test_valid_route_in_wrong_result_slot_is_rejected(slot, wrong_route):
    semantic = _semantic()
    if slot == 'overall':
        semantic['overall']['expression_route'] = wrong_route
    else:
        semantic['categories'][slot]['expression_route'] = wrong_route
    with pytest.raises(selection.FortuneSelectionError):
        selection.select_variants(semantic, today=TODAY)
