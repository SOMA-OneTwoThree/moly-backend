"""프로세스 공통 로깅 설정 — API(uvicorn)·consumer·worker가 같은 포맷(JSON 1줄, stderr)을 쓴다.

- root = settings.log_level(INFO), 핸들러 1개(stderr). 앱 로거는 전부 root로 propagate.
  설정이 없으면 root에 핸들러가 없어 INFO는 버려지고 WARNING은 레벨·로거명 없이 찍힌다.
- uvicorn / uvicorn.error / uvicorn.access 는 uvicorn이 붙인 핸들러를 떼고 root로 보낸다 —
  한 줄이 두 번 찍히지 않고 포맷이 하나가 된다. uvicorn은 앱 import 전에 자기 설정을 적용하므로
  앱 팩토리에서 부르는 이 설정이 나중에 이긴다.
- httpx·httpcore = WARNING. INFO는 요청 URL 전체를 찍어 웹훅·핑 URL 같은 비밀값이 로그에 남는다.
- ELB 헬스체크(`GET /health` 200)는 access 로그에서 뺀다. 다른 상태코드·경로(/health/ready 포함)는 남긴다.
- `extra=`로 넘긴 필드는 JSON 최상위에 그대로 실린다.
- stdout은 쓰지 않는다 — consumer의 STARTUP_CHECK_ONLY가 stdout에 핸들러 목록을 print 한다.
"""
from __future__ import annotations

import json
import logging
import logging.config
import time
from typing import Any

from app.config import settings

# LogRecord 기본 속성 — extra 필드를 골라낼 때 제외한다.
_RESERVED: frozenset[str] = frozenset(
    vars(logging.LogRecord("x", logging.INFO, "x", 0, "", (), None))
) | {"message", "asctime", "taskName", "color_message"}  # color_message: uvicorn 내부용 extra


def _access_args(record: logging.LogRecord) -> tuple | None:
    """uvicorn.access 레코드의 (client, method, path, http_version, status). 모양이 다르면 None."""
    args = record.args
    if record.name == "uvicorn.access" and isinstance(args, tuple) and len(args) == 5:
        return args
    return None


class JsonFormatter(logging.Formatter):
    """한 레코드 = JSON 한 줄. ts(UTC)·level·logger·msg + extra + 예외 텍스트.

    uvicorn.access 레코드는 client·method·path·status를 필드로 풀어 넣어 상태코드·경로 집계가
    문자열 파싱 없이 되게 한다.
    """

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        access = _access_args(record)
        if access is not None:
            client, method, path, _http_version, status = access
            out.update({"client": client, "method": method, "path": path, "status": status})
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            out["stack"] = self.formatStack(record.stack_info)
        return json.dumps(out, ensure_ascii=False, default=str)


class DropHealthAccess(logging.Filter):
    """ELB 헬스체크 `GET /health` 200 액세스 로그만 버린다(실패 응답과 다른 경로는 남긴다)."""

    def filter(self, record: logging.LogRecord) -> bool:
        access = _access_args(record)
        return not (
            access is not None
            and access[1] == "GET" and access[2] == "/health" and access[4] == 200
        )


_configured = False


def configure_logging(*, force: bool = False) -> None:
    """root 로깅을 1회 설정한다(멱등). 다시 부르면 force 없이는 아무것도 하지 않는다."""
    global _configured
    if _configured and not force:
        return
    formatter: dict[str, Any] = (
        {"()": "app.core.logging_setup.JsonFormatter"}
        if settings.log_json
        else {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}
    )
    access_filters = [] if settings.log_access_health else ["drop_health"]
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"std": formatter},
        "filters": {"drop_health": {"()": "app.core.logging_setup.DropHealthAccess"}},
        "handlers": {
            "stderr": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
                "formatter": "std",
            },
        },
        "root": {"level": str(settings.log_level).upper(), "handlers": ["stderr"]},
        "loggers": {
            # uvicorn 자체 핸들러 제거 → root 한 곳으로(중복 출력 없음, 포맷 통일).
            "uvicorn": {"handlers": [], "propagate": True, "level": "INFO"},
            "uvicorn.error": {"handlers": [], "propagate": True, "level": "INFO"},
            "uvicorn.access": {
                "handlers": [], "propagate": True, "level": "INFO", "filters": access_filters,
            },
            # INFO에서 요청 URL 전체를 찍는다(웹훅 경로·핑 UUID 노출) → WARNING.
            "httpx": {"level": "WARNING"},
            "httpcore": {"level": "WARNING"},
            # mem0는 INFO가 수다스럽다(벡터 스토어 연결 로그). 문제는 WARNING 이상으로 충분.
            "mem0": {"level": "WARNING"},
        },
    })
    _configured = True
