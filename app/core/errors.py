"""공통 에러 규약 — API_SPEC 1장 형식 + 부록 B 비즈니스 코드.

응답 형식: {"error": {"code", "message", "details"}}
HTTP: 400 형식 / 401 미인증 / 402 건초부족 / 403 플랜게이트 / 404 없음 /
      409 상태충돌 / 422 검증실패 / 429 횟수상한 / 5xx 서버.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    """비즈니스 에러 — 핸들러가 API_SPEC 형식으로 직렬화."""

    def __init__(
        self,
        code: str,
        http_status: int,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.http_status = http_status
        self.message = message
        self.details = details or {}
        super().__init__(message)


def _body(code: str, message: str, details: dict[str, Any]) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content=jsonable_encoder(_body(exc.code, exc.message, exc.details)),
    )


async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder(
            _body("VALIDATION", "요청 형식이 올바르지 않습니다.", {"errors": exc.errors()})
        ),
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)


# --- 부록 B 비즈니스 에러 팩토리 (모듈에서 raise 해서 사용) ---
def daily_limit_reached() -> AppError:
    return AppError("DAILY_LIMIT_REACHED", 403, "오늘의 대화 한도를 모두 사용했어요.")


def subscriber_only() -> AppError:
    return AppError("SUBSCRIBER_ONLY", 403, "구독 전용 기능이에요.")


def insufficient_hay(required: int, balance: int) -> AppError:
    return AppError(
        "INSUFFICIENT_HAY", 402, "건초가 부족합니다.", {"required": required, "balance": balance}
    )


def already_claimed() -> AppError:
    return AppError("ALREADY_CLAIMED", 409, "이미 수령했어요.")


def already_owned() -> AppError:
    return AppError("ALREADY_OWNED", 409, "이미 보유 중이에요.")


def already_processed() -> AppError:
    return AppError("ALREADY_PROCESSED", 409, "이미 처리된 거래예요.")


def restore_conflict() -> AppError:
    return AppError("RESTORE_CONFLICT", 409, "다른 계정에 연결된 구독이에요.")


def routine_goal_not_met() -> AppError:
    return AppError("ROUTINE_GOAL_NOT_MET", 422, "루틴 2개를 아직 다 완료하지 않았어요.")


def ad_limit_reached() -> AppError:
    return AppError("AD_LIMIT_REACHED", 429, "오늘 광고 시청 한도에 도달했어요.")


def ad_verify_failed() -> AppError:
    return AppError("AD_VERIFY_FAILED", 422, "광고 시청 확인에 실패했어요.")


def receipt_invalid() -> AppError:
    return AppError("RECEIPT_INVALID", 422, "영수증 검증에 실패했어요.")


def not_owned() -> AppError:
    return AppError("NOT_OWNED", 422, "보유하지 않은 아이템이에요.")


def validation(message: str = "요청 형식이 올바르지 않습니다.", details: dict[str, Any] | None = None) -> AppError:
    return AppError("VALIDATION", 422, message, details)
