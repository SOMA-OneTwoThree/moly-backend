"""바깥 DO 전에 타이머를 설정하며 단일 TX·rollback·취소 동작을 보존한다."""
import argparse
import asyncio
from unittest.mock import AsyncMock

import pytest

from db import apply as runner


class Transaction:
    def __init__(self, connection):
        self.connection = connection
        self.start = AsyncMock(side_effect=self._start)
        self.commit = AsyncMock(side_effect=self._finish)
        self.rollback = AsyncMock(side_effect=self._finish)

    def _start(self):
        self.connection.active = True

    def _finish(self):
        self.connection.active = False


class Connection:
    def __init__(self, failure=None):
        self.active = False
        self.statements = []
        self.failure = failure
        self.tx = Transaction(self)
        self.close = AsyncMock()

    def transaction(self):
        return self.tx

    def is_in_transaction(self):
        return self.active

    async def execute(self, sql):
        self.statements.append(sql)
        if sql.startswith('DO ') and self.failure is not None:
            raise self.failure


@pytest.mark.parametrize('timeout,commit', [(60_000, False), (750, False), (750, True)])
async def test_timeout_is_set_before_one_outer_do_and_preserves_transaction(
        monkeypatch, tmp_path, timeout, commit):
    path = tmp_path / 'reviewed.sql'
    path.write_text('CREATE TABLE public.synthetic(id int);')
    connection = Connection()
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr(runner.asyncpg, 'connect', connect)
    monkeypatch.setattr(runner, 'load_conn', lambda env: 'postgresql://synthetic')
    monkeypatch.setattr(runner, 'announce', lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, 'assert_dev_target', lambda *args: None)
    kwargs = {} if timeout == 60_000 else {'statement_timeout_ms': timeout}
    await runner.apply(path, 'dev', commit=commit, **kwargs)
    statements = connection.statements
    assert statements[0] == "SET LOCAL lock_timeout = '2s'"
    assert statements[1] == f"SET LOCAL statement_timeout = '{timeout}ms'"
    assert len([sql for sql in statements if sql.startswith('DO ')]) == 1
    assert statements[-1].startswith('DO ')
    assert 'CREATE TABLE public.synthetic(id int);' in statements[-1]
    connection.tx.start.assert_awaited_once()
    (connection.tx.commit if commit else connection.tx.rollback).assert_awaited_once()
    (connection.tx.rollback if commit else connection.tx.commit).assert_not_awaited()
    connection.close.assert_awaited_once()


@pytest.mark.parametrize('failure', [RuntimeError('synthetic'), asyncio.CancelledError()])
async def test_error_and_cancellation_roll_back_and_close(monkeypatch, tmp_path, failure):
    path = tmp_path / 'reviewed.sql'
    path.write_text('SELECT 1;')
    connection = Connection(failure)
    monkeypatch.setattr(runner.asyncpg, 'connect', AsyncMock(return_value=connection))
    monkeypatch.setattr(runner, 'load_conn', lambda env: 'postgresql://synthetic')
    monkeypatch.setattr(runner, 'announce', lambda *args, **kwargs: None)
    with pytest.raises(type(failure)):
        await runner.apply(path, 'dev', statement_timeout_ms=750)
    connection.tx.rollback.assert_awaited_once()
    connection.tx.commit.assert_not_awaited()
    connection.close.assert_awaited_once()


@pytest.mark.parametrize('value', [0, -1, True, 1.5, '750', None])
async def test_invalid_timeout_fails_before_file_or_connection(monkeypatch, tmp_path, value):
    connect = AsyncMock()
    monkeypatch.setattr(runner.asyncpg, 'connect', connect)
    with pytest.raises(ValueError, match='positive integer'):
        await runner.apply(tmp_path / 'absent.sql', 'dev', statement_timeout_ms=value)
    connect.assert_not_awaited()


@pytest.mark.parametrize('value', ['0', '-1', '1.5', 'bad'])
def test_cli_timeout_requires_positive_integer(value):
    with pytest.raises(argparse.ArgumentTypeError):
        runner._positive_timeout(value)


def test_cli_passes_explicit_timeout_before_execution(monkeypatch, tmp_path):
    apply = AsyncMock()
    monkeypatch.setattr(runner, 'apply', apply)
    monkeypatch.setattr('sys.argv', ['db.apply', str(tmp_path / 'reviewed.sql'),
                                   '--env', 'dev', '--statement-timeout-ms', '750'])
    assert runner.main() == 0
    assert apply.await_args.kwargs['statement_timeout_ms'] == 750
