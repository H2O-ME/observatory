"""Unified error codes and FastAPI handlers (spec §5.5).

All application errors carry ``{code, message, retryable}`` plus the server time.
The HTTP status mapping is defined once here so routers never guess.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .clock import now_ms


class ErrorCode(StrEnum):
    AUTH = "AUTH"
    GONE = "GONE"
    BAD_REQUEST = "BAD_REQUEST"
    UNSUPPORTED = "UNSUPPORTED"
    TIMEOUT = "TIMEOUT"
    RATE = "RATE"
    INTERNAL = "INTERNAL"


CODE_TO_STATUS: dict[ErrorCode, int] = {
    ErrorCode.AUTH: 401,
    ErrorCode.GONE: 410,
    ErrorCode.BAD_REQUEST: 422,
    ErrorCode.UNSUPPORTED: 422,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.RATE: 429,
    ErrorCode.INTERNAL: 500,
}

AUTH = ErrorCode.AUTH
GONE = ErrorCode.GONE
BAD_REQUEST = ErrorCode.BAD_REQUEST
UNSUPPORTED = ErrorCode.UNSUPPORTED
TIMEOUT = ErrorCode.TIMEOUT
RATE = ErrorCode.RATE
INTERNAL = ErrorCode.INTERNAL


class AppError(Exception):
    """An error that the handlers can render without losing its meaning."""

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        retryable: bool = False,
        http_status: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = ErrorCode(code)
        self.message = str(message)
        self.retryable = bool(retryable)
        self._http_status = http_status
        #: Seconds a rate-limited caller should wait before retrying (``Retry-After``).
        self.retry_after = max(1, int(retry_after)) if retry_after else None

    @property
    def http_status(self) -> int:
        return self._http_status or CODE_TO_STATUS[self.code]

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"

    # -- constructors ------------------------------------------------------
    @classmethod
    def auth(cls, message: str = "未认证或会话已过期", *, http_status: int | None = 401) -> AppError:
        return cls(AUTH, message, http_status=http_status)

    @classmethod
    def gone(cls, message: str = "资源已不存在") -> AppError:
        return cls(GONE, message)

    @classmethod
    def bad_request(cls, message: str = "请求不合法") -> AppError:
        return cls(BAD_REQUEST, message)

    @classmethod
    def unsupported(cls, message: str = "不支持的操作") -> AppError:
        return cls(UNSUPPORTED, message)

    @classmethod
    def timeout(cls, message: str = "操作超时", *, retryable: bool = True) -> AppError:
        return cls(TIMEOUT, message, retryable=retryable)

    @classmethod
    def rate(cls, message: str = "请求过于频繁", *, retry_after: int | None = None) -> AppError:
        return cls(RATE, message, retryable=True, retry_after=retry_after)

    @classmethod
    def internal(cls, message: str = "服务器内部错误", *, retryable: bool = True) -> AppError:
        return cls(INTERNAL, message, retryable=retryable)


def error_payload(error: AppError) -> dict[str, Any]:
    """The wire shape shared by every error response."""

    return {
        "code": error.code.value,
        "message": error.message,
        "retryable": error.retryable,
        "server_time_ms": now_ms(),
    }


_STATUS_TO_CODE: dict[int, ErrorCode] = {
    400: BAD_REQUEST,
    401: AUTH,
    403: AUTH,
    404: BAD_REQUEST,
    405: UNSUPPORTED,
    409: BAD_REQUEST,
    410: GONE,
    422: BAD_REQUEST,
    429: RATE,
    504: TIMEOUT,
}


def _http_error_to_app_error(exc: StarletteHTTPException) -> AppError:
    code = _STATUS_TO_CODE.get(exc.status_code, INTERNAL)
    detail = exc.detail
    message = detail if isinstance(detail, str) else "请求失败"
    retryable = exc.status_code in {429, 500, 502, 503, 504}
    return AppError(code, message, retryable=retryable, http_status=exc.status_code)


def install_error_handlers(app: FastAPI) -> None:
    """Register the three handlers that make errors uniform."""

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        return JSONResponse(error_payload(exc), status_code=exc.http_status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        message = str(first.get("msg") or "请求参数不合法")
        if location:
            message = f"{location}: {message}"
        error = AppError.bad_request(message)
        return JSONResponse(error_payload(error), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        error = _http_error_to_app_error(exc)
        return JSONResponse(error_payload(error), status_code=error.http_status)


__all__ = [
    "AUTH",
    "BAD_REQUEST",
    "CODE_TO_STATUS",
    "ErrorCode",
    "GONE",
    "INTERNAL",
    "RATE",
    "TIMEOUT",
    "UNSUPPORTED",
    "AppError",
    "error_payload",
    "install_error_handlers",
]