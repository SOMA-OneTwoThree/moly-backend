"""The catalog gate must preserve policies and detect executable schema drift."""
from copy import deepcopy

import pytest

from db.catalog import compare_catalog
from db.schema_contract import load_contract, require_scratch


def test_generated_contract_matches_schema_source():
    assert 'public.products' in load_contract()['tables']


@pytest.mark.parametrize(('section', 'key', 'field', 'value'), [
    ('columns', 'public.products.price_hay', 'not_null', True),
    ('columns', 'public.products.price_hay', 'type', 'text'),
    ('columns', 'public.profiles.hay_balance', 'default', '100'),
    ('tables', 'public.fortune_profiles', 'relrowsecurity', False),
    ('constraints', 'public.products.products_hay_pack_ck', 'definition', 'CHECK (true)'),
    ('indexes', 'public.messages_fortune_context_root_idx', 'valid', False),
    ('triggers', 'auth.users.on_auth_user_created', 'enabled', 'D'),
    ('functions', 'public.bootstrap_user(uuid,timestamp with time zone)', 'owner', 'anon'),
])
def test_detects_behavioral_and_security_drift(section, key, field, value):
    expected = load_contract()
    changed = deepcopy(expected)
    changed[section][key][field] = value
    assert f'changed {section}: {key}' in compare_catalog(expected, changed)


def test_missing_objects_and_extra_sensitive_grants_fail():
    expected = load_contract()
    changed = deepcopy(expected)
    del changed['indexes']['public.reward_ad_sessions_expiry_idx']
    changed['relation_grants']['public.fortune_profiles:anon:SELECT'] = {'grantable': False}
    problems = compare_catalog(expected, changed)
    assert 'missing indexes: public.reward_ad_sessions_expiry_idx' in problems
    assert 'unexpected relation_grants: public.fortune_profiles:anon:SELECT' in problems


def test_additive_rollout_objects_are_allowed_only_outside_strict_mode():
    expected = load_contract()
    changed = deepcopy(expected)
    changed['tables']['public.future_table'] = {'relrowsecurity': True}
    assert compare_catalog(expected, changed) == []
    assert compare_catalog(expected, changed, strict=True) == [
        'unexpected tables: public.future_table',
    ]


@pytest.mark.parametrize('dsn', [
    'postgresql://postgres@example.com/moly_schema_test',
    'postgresql://postgres@localhost/production',
    'postgresql://postgres@127.0.0.1/postgres',
    'postgresql://postgres@localhost/moly_schema_test?host=example.com',
    'postgresql://postgres@localhost/moly_schema_test?dbname=production',
    'postgresql://postgres@localhost/moly_schema_test%2Fproduction',
])
def test_generation_rejects_non_disposable_targets(dsn):
    with pytest.raises(ValueError):
        require_scratch(dsn)
