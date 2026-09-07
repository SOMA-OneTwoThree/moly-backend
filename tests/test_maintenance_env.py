"""CLI DB selection cannot merely relabel an already configured application."""
import pytest

from app.config import settings
from app.core import db
from db.envfile import bootstrap_script_environment, configure_application_db, load_conn


def test_exact_env_key_quotes_and_application_engine_target(tmp_path, monkeypatch):
    env = tmp_path / 'review.env'
    env.write_text('SUPABASE_DB_CONNECTION_STRING_BACKUP=wrong\n'
                   'export SUPABASE_DB_CONNECTION_STRING="postgresql://tester@localhost/moly_schema_target" # selected\n')
    monkeypatch.setattr(db, '_engine', None)
    monkeypatch.setattr(db, '_sessionmaker', None)
    monkeypatch.setattr(settings, 'supabase_db_connection_string', 'postgresql://wrong/target')
    selected = configure_application_db(str(env))
    assert selected == load_conn(str(env))
    assert db._async_dsn() == 'postgresql+asyncpg://tester@localhost/moly_schema_target'


@pytest.mark.parametrize('active', ['_engine', '_sessionmaker'])
def test_refuses_target_switch_after_pool_creation(tmp_path, monkeypatch, active):
    env = tmp_path / 'review.env'
    env.write_text('SUPABASE_DB_CONNECTION_STRING=postgresql://selected/target\n')
    monkeypatch.setattr(db, '_engine', None)
    monkeypatch.setattr(db, '_sessionmaker', None)
    monkeypatch.setattr(db, active, object())
    monkeypatch.setattr(settings, 'supabase_db_connection_string', 'postgresql://original/target')
    with pytest.raises(RuntimeError, match='before creating an engine'):
        configure_application_db(str(env))
    assert settings.supabase_db_connection_string == 'postgresql://original/target'


def test_explicit_provider_env_overrides_inherited_env(monkeypatch):
    import os
    monkeypatch.setenv('MOLY_ENV_FILE', '.env.prod')
    bootstrap_script_environment(['--env', 'dev'])
    assert os.environ['MOLY_ENV_FILE'] == '.env'


def test_db_only_env_read_preserves_literal_password_and_ignores_unrelated_notes(tmp_path, caplog):
    env = tmp_path / 'review.env'
    env.write_text('This is an unrelated note with spaces\n'
                   'SUPABASE_DB_CONNECTION_STRING="postgresql://tester:${not_expanded}@localhost/test"\n')
    assert load_conn(str(env)) == 'postgresql://tester:${not_expanded}@localhost/test'
    assert not caplog.records


@pytest.mark.parametrize('dsn', [
    'postgresql://postgres.wywzjslvxwttxkecbyis:pw@localhost/postgres?user=postgres.otherproject',
    'postgresql://postgres.wywzjslvxwttxkecbyis:pw@localhost/postgres?host=another-host',
    'postgresql://postgres.wywzjslvxwttxkecbyis:pw@localhost/postgres?dbname=another-database',
    'postgresql://admin:postgres.wywzjslvxwttxkecbyis:pw@localhost/postgres',
])
def test_dev_guard_rejects_connection_overrides_and_ref_in_password(dsn):
    from db.envfile import assert_dev_target
    with pytest.raises(SystemExit, match='차단'):
        assert_dev_target('dev', dsn)


def test_dev_guard_accepts_only_the_expected_username_target():
    from db.envfile import assert_dev_target, project_ref
    dsn = 'postgresql://postgres.wywzjslvxwttxkecbyis:pw@localhost/postgres'
    assert_dev_target('dev', dsn)
    assert project_ref(dsn) == 'wywzjslvxwttxkecbyis'
    with pytest.raises(SystemExit):
        assert_dev_target('prod', dsn)
