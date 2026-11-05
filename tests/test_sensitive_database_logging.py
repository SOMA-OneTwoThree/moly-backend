"""Database-only sanitization on JSON and plain handlers; no real DB or secrets."""
import io
import json
import logging
import sys

import asyncpg
import pytest
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.core.logging_setup import SensitiveDatabaseError, JsonFormatter, configure_logging

SENSITIVE = 'synthetic-plain synthetic-cipher synthetic-key'


def database_error():
    driver = asyncpg.CheckViolationError('new row violates check; DETAIL: Failing row contains ' + SENSITIVE)
    return IntegrityError('INSERT INTO messages(content_ciphertext) VALUES (...) ' + SENSITIVE,
                          {'ciphertext': SENSITIVE}, driver, hide_parameters=True)


def record(error, *, style='trace'):
    if style == 'trace':
        try:
            try:
                raise error
            except Exception as cause:
                raise RuntimeError('wrapped ' + SENSITIVE) from cause
        except RuntimeError:
            value = logging.LogRecord('moly-backend', logging.ERROR, 'source.py', 1,
                'unhandled %r ' + SENSITIVE, (error,), sys.exc_info())
    elif style == 'args':
        value = logging.LogRecord('moly-worker', logging.ERROR, 'source.py', 1,
            'failure %r ' + SENSITIVE, (error,), None)
    elif style == 'dict':
        value = logging.LogRecord('moly-worker', logging.ERROR, 'source.py', 1,
            'failure %(error)r ' + SENSITIVE, ({'error': error},), None)
    else:
        value = logging.LogRecord('moly-worker', logging.ERROR, 'source.py', 1, error, (), None)
    value.request_body = SENSITIVE
    value.ciphertext = SENSITIVE
    value.exc_text = 'cached traceback ' + SENSITIVE
    value.stack_info = 'cached stack ' + SENSITIVE
    return value


@pytest.mark.parametrize('style', ['trace', 'args', 'dict', 'message'])
@pytest.mark.parametrize('driver_only', [True, False])
def test_database_failure_removes_server_detail_binds_chain_stack_and_all_extras(style, driver_only):
    error = database_error()
    if driver_only:
        error = error.orig
    value = record(error, style=style)
    assert SensitiveDatabaseError().filter(value)
    for formatter in (JsonFormatter(), logging.Formatter('%(name)s %(message)s')):
        output = formatter.format(value)
        assert all(word not in output for word in SENSITIVE.split())
        assert 'Failing row' not in output and 'INSERT INTO' not in output and 'Traceback' not in output
        assert 'database_error' in output and 'sqlstate=23514' in output
    assert value.exc_info is None and value.exc_text is None and value.stack_info is None
    assert not hasattr(value, 'request_body') and not hasattr(value, 'ciphertext')


def test_exception_object_and_retry_metadata_are_never_changed():
    error = database_error()
    original_args, original_params, original_driver = error.args, error.params, error.orig
    SensitiveDatabaseError().filter(record(error))
    assert error.args == original_args and error.params is original_params and error.orig is original_driver
    assert SENSITIVE in str(error.orig)


def test_only_database_exceptions_are_sanitized():
    error = ValueError('ordinary diagnostic')
    value = logging.LogRecord('moly-worker', logging.ERROR, 'source.py', 1, 'app %r', (error,), None)
    value.metric = {'count': 2}
    before = value.__dict__.copy()
    assert SensitiveDatabaseError().filter(value)
    assert value.__dict__ == before


def test_cyclic_causes_and_exception_group_are_safe():
    original = database_error()
    outer = RuntimeError('outer ' + SENSITIVE)
    outer.__cause__ = original
    original.__context__ = outer
    value = record(ExceptionGroup('group ' + SENSITIVE, [outer]), style='args')
    SensitiveDatabaseError().filter(value)
    assert 'synthetic-' not in JsonFormatter().format(value)


@pytest.mark.parametrize('json_mode', [False, True])
def test_configured_sink_filter_covers_both_formatters(monkeypatch, json_mode):
    monkeypatch.setattr(settings, 'log_json', json_mode)
    configure_logging(force=True)
    root = logging.getLogger()
    assert all(any(isinstance(f, SensitiveDatabaseError) for f in h.filters) for h in root.handlers)
    output = io.StringIO()
    root.handlers[0].setStream(output)
    try:
        error = database_error()
        logging.getLogger('moly-worker').error('SQL failure %r', error, extra={'body': SENSITIVE})
        rendered = output.getvalue()
        assert 'synthetic-' not in rendered and 'database_error' in rendered and '23514' in rendered
        if json_mode:
            assert set(json.loads(rendered)) == {'ts', 'level', 'logger', 'msg', 'error_type', 'sqlstate'}
    finally:
        configure_logging(force=True)
