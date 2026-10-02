"""프로세스 공통 로깅 — 세 프로세스(API·consumer·worker)가 같은 JSON 한 줄을 쓰고, 비밀값이 새지 않는다.

root에 핸들러가 없으면 INFO가 전부 버려지고 WARNING은 레벨·로거명 없이 찍힌다. httpx INFO는 요청 URL
전체(웹훅 경로·핑 UUID)를 찍는다. 이 둘이 이 모듈이 막는 것이다.
"""
from __future__ import annotations

import copy
import json
import logging
import logging.config
import sys

import pytest

from app.core.logging_setup import DropHealthAccess, JsonFormatter, configure_logging


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def root_capture():
    """configure_logging은 root 핸들러를 갈아 끼운다 — 그 뒤에 붙여야 propagate된 레코드를 본다."""
    configure_logging(force=True)
    cap = _Capture()
    root = logging.getLogger()
    root.addHandler(cap)
    try:
        yield cap
    finally:
        root.removeHandler(cap)


def _access(method: str, path: str, status: int) -> tuple:
    return ('%s - "%s %s HTTP/%s" %d', ("10.0.0.1:1", method, path, "1.1", status))


def _access_record(method: str, path: str, status: int) -> logging.LogRecord:
    msg, args = _access(method, path, status)
    return logging.LogRecord("uvicorn.access", logging.INFO, "h11_impl.py", 0, msg, args, None)


def test_root_logs_info_and_uvicorn_goes_through_root_once():
    configure_logging(force=True)
    root = logging.getLogger()
    assert len(root.handlers) == 1 and root.level == logging.INFO
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        assert lg.handlers == [] and lg.propagate is True, name  # 한 줄이 두 번 찍히지 않는다
    for name in ("moly-backend", "moly-worker", "moly"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.INFO, name


def test_httpx_info_is_dropped_so_request_urls_never_reach_the_log(root_capture):
    for name in ("httpx", "httpcore", "mem0"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING, name
    logging.getLogger("httpx").info('HTTP Request: POST https://hooks.example/services/T/B/X "200"')
    logging.getLogger("httpx").warning("httpx 경고는 남는다")
    msgs = [r.getMessage() for r in root_capture.records]
    assert msgs == ["httpx 경고는 남는다"]


def test_uvicorn_settings_applied_first_are_replaced():
    """uvicorn은 앱 import 전에 자기 dictConfig를 적용한다. 나중에 부르는 이쪽이 핸들러를 떼어야 한다."""
    from uvicorn.config import LOGGING_CONFIG

    logging.config.dictConfig(copy.deepcopy(LOGGING_CONFIG))
    assert logging.getLogger("uvicorn.access").handlers  # uvicorn 자체 핸들러가 붙은 상태
    configure_logging(force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        assert lg.handlers == [] and lg.propagate is True, name


def test_elb_health_200_is_the_only_access_line_dropped(root_capture):
    access = logging.getLogger("uvicorn.access")
    for method, path, status in [
        ("GET", "/health", 200), ("GET", "/health", 503), ("GET", "/health/ready", 200),
        ("POST", "/chat/messages", 503),
    ]:
        msg, args = _access(method, path, status)
        access.info(msg, *args)
    seen = [r.args[1:3] + (r.args[4],) for r in root_capture.records if r.name == "uvicorn.access"]
    assert seen == [("GET", "/health", 503), ("GET", "/health/ready", 200),
                    ("POST", "/chat/messages", 503)]
    f = DropHealthAccess()
    assert f.filter(_access_record("GET", "/health", 200)) is False
    plain = logging.LogRecord("moly-backend", logging.INFO, "x.py", 1, "GET /health", (), None)
    assert f.filter(plain) is True  # access 레코드 모양이 아니면 건드리지 않는다


def test_json_line_has_level_logger_extra_and_access_fields():
    fmt = JsonFormatter()
    rec = logging.LogRecord(
        "moly-backend", logging.INFO, "x.py", 1, "chat_turn_metrics %s", ('{"total_ms": 1234}',), None
    )
    rec.event = "chat_turn_metrics"
    rec.metrics = {"total_ms": 1234}
    out = json.loads(fmt.format(rec))
    assert out["level"] == "INFO" and out["logger"] == "moly-backend"
    assert out["msg"] == 'chat_turn_metrics {"total_ms": 1234}'
    assert out["event"] == "chat_turn_metrics" and out["metrics"] == {"total_ms": 1234}
    assert out["ts"].endswith("Z")
    acc = json.loads(fmt.format(_access_record("POST", "/chat/messages", 503)))
    assert (acc["method"], acc["path"], acc["status"]) == ("POST", "/chat/messages", 503)
    assert acc["msg"] == '10.0.0.1:1 - "POST /chat/messages HTTP/1.1" 503'


def test_traceback_stays_on_one_line():
    fmt = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        rec = logging.LogRecord("moly-backend", logging.ERROR, "x.py", 1, "unhandled", (), sys.exc_info())
    line = fmt.format(rec)
    assert len(line.splitlines()) == 1
    assert "ValueError: boom" in json.loads(line)["exc"]


def test_configure_is_idempotent_without_force():
    configure_logging(force=True)
    handler = logging.getLogger().handlers[0]
    configure_logging()
    assert logging.getLogger().handlers == [handler]
