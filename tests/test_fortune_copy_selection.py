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
    expected = ('v02', 'v11', 'v05', 'v03', 'v15', 'v08', 'v01', 'v18', 'v17', 'v19', 'v13', 'v14', 'v10', 'v12', 'v06', 'v16', 'v09', 'v04', 'v07', 'v20')
    assert selection.variant_order('overall.d50.general') == expected
    selection.variant_order.cache_clear()
    assert selection.variant_order('overall.d50.general') == expected
    # Presentation selection has no user, language, or request-clock input.
    assert set(inspect.signature(selection.select_variants).parameters) == {
        'semantic', 'today', 'previous_semantic'
    }


@pytest.mark.parametrize('route', sorted(selection.ALL_ROUTES))
def test_every_route_has_twenty_unique_variants_then_repeats_on_21st_advance(route):
    semantic = _semantic()
    slot = 'overall' if route.startswith('overall.') else route.split('.')[1]
    if slot == 'overall':
        semantic['overall']['expression_route'] = f"overall.{route.split('.')[1]}.start.default"
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
    assert third['copy_selection']['routes']['overall.d50.general']['position'] == 2


def test_skipping_calendar_dates_advances_only_once_per_new_snapshot():
    semantic = _semantic()
    first = _snapshot(semantic)
    later = _snapshot(semantic, TODAY + timedelta(days=40), first)
    assert later['copy_selection']['routes']['overall.d50.general']['position'] == 1


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
    output['routes']['overall.d50.general']['position'] = 19
    output['selected']['overall'] = 'v20'
    assert previous == original_previous
    assert semantic == original_semantic


def test_all_fifty_routes_can_be_retained_but_no_extra_route_is_allowed():
    previous = _snapshot(_semantic())
    previous['copy_selection']['routes'] = {
        route: {'day': TODAY.isoformat(), 'position': 0} for route in selection.ALL_ROUTES
    }
    assert len(previous['copy_selection']['routes']) == 50
    result = selection.select_variants(_semantic(), today=TODAY + timedelta(days=1), previous_semantic=previous)
    assert len(result['routes']) == 50
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
    previous['copy_selection']['routes']['overall.d50.general']['position'] = position
    with pytest.raises(selection.FortuneSelectionError):
        selection.select_variants(_semantic(), today=TODAY, previous_semantic=previous)


@pytest.mark.parametrize('day', ['2026-02-30', '20260908', '2026-W37-2', '2026-9-8', '', None, TODAY])
def test_rejects_invalid_or_noncanonical_cursor_date(day):
    previous = _snapshot(_semantic())
    previous['copy_selection']['routes']['overall.d50.general']['day'] = day
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
        state['routes']['overall.d50.general']['count'] = 1
    with pytest.raises(selection.FortuneSelectionError):
        selection.select_variants(_semantic(), today=TODAY, previous_semantic=previous)


@pytest.mark.parametrize('mutation', ['missing_current_cursor', 'mismatched_selected'])
def test_rejects_state_inconsistent_with_previously_selected_snapshot(mutation):
    previous = _snapshot(_semantic())
    state = previous['copy_selection']
    if mutation == 'missing_current_cursor':
        del state['routes']['overall.d50.general']
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


@pytest.mark.parametrize('flow', selection.FLOW_KEYS)
def test_calculation_flows_share_one_score_band_cursor(flow):
    first = _snapshot(_semantic('overall.d50.start.default'))
    changed = _semantic(f'overall.d50.{flow}.default')
    same_day = _snapshot(changed, TODAY, first)
    assert same_day['copy_selection'] == first['copy_selection']
    next_day = _snapshot(changed, TODAY + timedelta(days=1), same_day)
    assert next_day['copy_selection']['routes']['overall.d50.general']['position'] == 1
    assert len(next_day['copy_selection']['routes']) == 5


def _legacy_snapshot():
    from hashlib import sha256

    snapshot = _semantic()
    routes = {'overall': snapshot['overall']['expression_route']}
    routes.update({k: v['expression_route'] for k, v in snapshot['categories'].items()})
    # Independent old-format fixture, including dormant overall/category cursors.
    positions = {route: {'day': TODAY.isoformat(), 'position': 7} for route in routes.values()}
    positions['overall.d20.start.default'] = {'day': TODAY.isoformat(), 'position': 13}
    positions['category.love.d20.general'] = {'day': TODAY.isoformat(), 'position': 13}
    chosen = {}
    for key, route in routes.items():
        order = sorted(selection.VARIANT_IDS, key=lambda v: (
            sha256(f'fortune-selection.v1|{route}|{v}'.encode('ascii')).digest(), v,
        ))
        chosen[key] = order[7]
    snapshot['copy_selection'] = {'version': 'fortune-selection.v1', 'selected': chosen, 'routes': positions}
    return snapshot


@pytest.mark.parametrize('days', [-1, 0, 1])
def test_legacy_upgrade_validates_then_resets_all_rewritten_copy_pools(days):
    from app.services.fortune_scores import generate_result

    old = _legacy_snapshot()
    original = deepcopy(old)
    current = generate_result(birth_date=date(2002, 12, 13), local_date=TODAY + timedelta(days=days))
    state = selection.select_variants(current, today=TODAY + timedelta(days=days), previous_semantic=old)
    assert old == original
    assert state['version'] == selection.SELECTION_VERSION
    assert len(state['routes']) == 5
    assert all(cursor['position'] == 0 for cursor in state['routes'].values())
    assert not any(route.endswith('.default') for route in state['routes'])


def test_v2_snapshot_upgrades_to_independent_routes_without_mutating_old_result():
    from app.services.fortune_scores import generate_result
    old = _semantic()
    routes = {'overall': 'overall.d50.general', **{k: f'category.{k}.d50.general' for k in selection.CATEGORY_KEYS}}
    positions = {r: {'day': TODAY.isoformat(), 'position': 7} for r in routes.values()}
    chosen = {k: selection._order(r, 'fortune-selection.v1' if k != 'overall' else 'fortune-selection.v2')[7] for k, r in routes.items()}
    old['copy_selection'] = {'version': 'fortune-selection.v2', 'selected': chosen, 'routes': positions}
    original = deepcopy(old)
    fresh = generate_result(birth_date=date(2002, 12, 13), local_date=TODAY + timedelta(days=1))
    state = selection.select_variants(fresh, today=TODAY + timedelta(days=1), previous_semantic=old)
    assert old == original
    assert state['version'] == selection.SELECTION_VERSION
    assert len(state['routes']) == 5
    assert all(c['position'] == 0 for c in state['routes'].values())


@pytest.mark.parametrize('defect', ['selected', 'position', 'missing', 'unknown_route', 'version'])
def test_corrupt_legacy_state_is_rejected_before_any_reset(defect):
    old = _legacy_snapshot()
    state = old['copy_selection']
    if defect == 'selected':
        old_id = state['selected']['overall']
        state['selected']['overall'] = next(v for v in selection.VARIANT_IDS if v != old_id)
    elif defect == 'position':
        state['routes']['overall.d20.start.default']['position'] = 20
    elif defect == 'missing':
        del state['routes']['overall.d50.balance.default']
    elif defect == 'unknown_route':
        state['routes']['overall.d20.invalid.default'] = {'day': TODAY.isoformat(), 'position': 0}
    else:
        state['version'] = 'fortune-selection.v999'
    with pytest.raises(selection.FortuneSelectionError):
        selection.select_variants(_semantic(), today=TODAY, previous_semantic=old)
