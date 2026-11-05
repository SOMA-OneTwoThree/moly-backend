"""Render the deployed daily report read-only; emit only typed failure metadata."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.config import settings  # noqa: E402
from app.core import db  # noqa: E402
from worker import monitoring, daily_report  # noqa: E402

DIAGNOSTIC_FORMAT = 'daily-report-diagnostics-v1'
PHASES = frozenset({'runtime', 'engine', 'rc_snapshot', 'daily_message', 'metric', 'validation', 'deadline', 'cleanup'})
METRICS = frozenset({'signups', 'activity', 'subscriptions', 'diaries', 'cost', 'worker_state',
                     'queue_state', 'subscription_comparison', 'none'})
EXCEPTIONS = frozenset({'TimeoutError', 'ValueError', 'RuntimeError', 'ConnectionError', 'OSError',
    'DBAPIError', 'ProgrammingError', 'OperationalError', 'InterfaceError', 'InternalError',
    'IntegrityError', 'DataError', 'InvalidRequestError', 'ArgumentError', 'QueryCanceledError',
    'LockNotAvailableError', 'UndefinedTableError', 'UndefinedColumnError', 'InsufficientPrivilegeError',
    'CannotConnectNowError', 'ConnectionDoesNotExistError', 'TooManyConnectionsError', 'OtherError'})
MAX_FAILURES, MAX_ELAPSED_MS = 16, 120_000
FAILURE_KEYS = frozenset({'phase', 'metric', 'exception', 'sqlstate', 'elapsed_ms', 'attempt', 'retry'})
_LOG_TEMPLATE = ('report_query metric=%s day=%s outcome=failed error=%s sqlstate=%s '
                 'elapsed_ms=%d attempt=%d retry=%s')


def valid_failure(value):
    return (isinstance(value, dict) and set(value) == FAILURE_KEYS
        and all(isinstance(value[k], str) for k in ('phase', 'metric', 'exception', 'sqlstate'))
        and value['phase'] in PHASES and value['metric'] in METRICS
        and value['exception'] in EXCEPTIONS and isinstance(value['sqlstate'], str)
        and (value['sqlstate'] in {'none', 'other'} or re.fullmatch(r'[0-9A-Z]{5}', value['sqlstate']) is not None)
        and type(value['elapsed_ms']) is int and 0 <= value['elapsed_ms'] <= MAX_ELAPSED_MS
        and type(value['attempt']) is int and 1 <= value['attempt'] <= 2
        and type(value['retry']) is bool)


def safe_report_result(value, *, environment, version):
    """Return a new whitelisted object, never arbitrary subprocess output."""
    base = {'daily_report', 'environment', 'version', 'read_only', 'slack_sent', 'elapsed_ms'}
    if (not isinstance(value, dict) or value.get('environment') != environment or value.get('version') != version
            or value.get('read_only') is not True or value.get('slack_sent') is not False
            or type(value.get('elapsed_ms')) is not int or not 0 <= value['elapsed_ms'] <= MAX_ELAPSED_MS):
        raise ValueError('daily_report_metadata_invalid')
    if value.get('daily_report') == 'ok' and set(value) == base:
        return dict(value)
    if (value.get('daily_report') != 'failed'
            or set(value) != base | {'error', 'diagnostic_format', 'failures', 'diagnostics_truncated'}
            or not isinstance(value['error'], str) or value['error'] not in EXCEPTIONS or value['diagnostic_format'] != DIAGNOSTIC_FORMAT
            or type(value['diagnostics_truncated']) is not bool
            or not isinstance(value['failures'], list) or not 1 <= len(value['failures']) <= MAX_FAILURES
            or not all(valid_failure(row) for row in value['failures'])):
        raise ValueError('daily_report_metadata_invalid')
    return {**{key: value[key] for key in base}, 'error': value['error'],
        'diagnostic_format': DIAGNOSTIC_FORMAT, 'failures': [dict(row) for row in value['failures']],
        'diagnostics_truncated': value['diagnostics_truncated']}


class MetricFailureCapture(logging.Handler):
    """CLI-local handler: typed log_failure args, never formatted log strings."""
    def __init__(self):
        super().__init__(logging.WARNING)
        self.rows = []
        self.truncated = False

    def append(self, row):
        if len(self.rows) == MAX_FAILURES:
            self.truncated = True
        else:
            self.rows.append(row)

    def emit(self, record):
        if (record.name != 'worker.daily_report' or record.msg != _LOG_TEMPLATE
                or not isinstance(record.args, tuple) or len(record.args) != 7):
            return
        metric, _, exception, state, elapsed, attempt, retry = record.args
        row = {'phase': 'metric', 'metric': metric, 'exception': exception,
               'sqlstate': state, 'elapsed_ms': elapsed, 'attempt': attempt, 'retry': retry}
        if valid_failure(row):
            self.append(row)


def _exception_name(error):
    name = type(error).__name__
    return name if name in EXCEPTIONS else 'OtherError'


def _elapsed(started):
    return min(MAX_ELAPSED_MS, max(0, int((monotonic()-started)*1000)))


def _outer_failure(phase, error, started):
    state = daily_report.sqlstate(error)
    if not isinstance(state, str) or (state != 'none' and not re.fullmatch(r'[A-Z0-9]{5}', state)):
        state = 'other'
    return {'phase': 'deadline' if isinstance(error, TimeoutError) else phase, 'metric': 'none',
            'exception': _exception_name(error), 'sqlstate': state,
            'elapsed_ms': _elapsed(started), 'attempt': 1, 'retry': False}


class DailyReportCheckError(ValueError):
    def __init__(self, result):
        self.result = result
        super().__init__('daily_report_check_failed')


async def check(environment: str, expected_sha: str, *, show: bool = False) -> None:
    started, phase = monotonic(), 'runtime'
    capture, owned_engine, failure = MetricFailureCapture(), None, None
    daily_report._log.addHandler(capture)
    try:
        if settings.environment != environment or settings.git_sha != expected_sha:
            raise ValueError('runtime mismatch')
        if not re.fullmatch(r'[0-9a-f]{40}', expected_sha):
            raise ValueError('invalid version')
        if db._engine is not None:
            raise ValueError('engine already initialized')
        phase = 'engine'
        db._engine = owned_engine = create_async_engine(db._async_dsn(), pool_size=4, max_overflow=0,
            hide_parameters=True, connect_args={'statement_cache_size': 0,
                'server_settings': {'default_transaction_read_only': 'on'}})
        async with asyncio.timeout(monitoring._MONITORING_BUDGET_S):
            now = datetime.now(timezone.utc)
            phase = 'rc_snapshot'
            async with db.get_sessionmaker()() as session:
                rc = await monitoring.rc_snapshot(session, now)
            phase = 'daily_message'
            message = await monitoring._daily_message(now, rc, persist_snapshot=False)
        phase = 'validation'
        for required in ('어제 신규 가입: ', 'DAU: ', 'MAU: ', '현재 구독 이용자: ', '개인 일기: 대상 '):
            if not re.search(re.escape(required)+r'[0-9]', message):
                raise ValueError('missing report metric')
        if any(fragment in message for fragment in (
            '집계 확인 필요', '집계 시간 초과', '집계 조회 실패', '상태 조회 확인 필요',
        )):
            raise ValueError('report query unavailable')
    except Exception as error:
        failure = _exception_name(error)
        capture.append(_outer_failure(phase, error, started))
    finally:
        try:
            if owned_engine is not None:
                await owned_engine.dispose()
        except Exception as error:
            failure = failure or _exception_name(error)
            capture.append(_outer_failure('cleanup', error, started))
        finally:
            daily_report._log.removeHandler(capture)
            capture.close()
    base = {'daily_report': 'failed' if failure else 'ok', 'environment': environment, 'version': expected_sha,
            'read_only': True, 'slack_sent': False, 'elapsed_ms': _elapsed(started)}
    if failure:
        raise DailyReportCheckError({**base, 'error': failure, 'diagnostic_format': DIAGNOSTIC_FORMAT,
            'failures': capture.rows, 'diagnostics_truncated': capture.truncated}) from None
    print(json.dumps(base))
    if show:
        print(message)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', required=True, choices=('development', 'production'))
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--show', action='store_true', help='print aggregate report preview')
    args = parser.parse_args()
    try:
        asyncio.run(check(args.environment, args.expected_sha, show=args.show))
    except DailyReportCheckError as error:
        print(json.dumps(error.result))
        raise SystemExit(1) from None
    except Exception as error:
        # Unexpected CLI initialization failures also never expose exception text.
        row = _outer_failure('runtime', error, monotonic())
        print(json.dumps({'daily_report': 'failed', 'environment': args.environment, 'version': args.expected_sha,
            'read_only': True, 'slack_sent': False, 'elapsed_ms': 0, 'error': row['exception'],
            'diagnostic_format': DIAGNOSTIC_FORMAT, 'failures': [row], 'diagnostics_truncated': False}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
