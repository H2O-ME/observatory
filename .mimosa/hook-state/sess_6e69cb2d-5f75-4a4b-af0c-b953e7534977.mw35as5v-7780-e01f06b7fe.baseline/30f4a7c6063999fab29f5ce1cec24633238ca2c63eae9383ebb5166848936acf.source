"""统一错误码（spec §5.5）：{code,message,retryable} + HTTP 映射。"""

from __future__ import annotations

import pytest

from observatory.core.errors import AppError, ErrorCode, error_payload

pytestmark = pytest.mark.unit

EXPECTED_MAPPING = {
    "AUTH": 401,
    "GONE": 410,
    "BAD_REQUEST": 422,
    "UNSUPPORTED": 422,
    "TIMEOUT": 504,
    "RATE": 429,
    "INTERNAL": 500,
}


def test_error_code_http_mapping() -> None:
    from observatory.core.errors import CODE_TO_STATUS

    assert {code.value: status for code, status in CODE_TO_STATUS.items()} == EXPECTED_MAPPING
    for code, status in EXPECTED_MAPPING.items():
        assert AppError(code, "x").http_status == status, code


def test_error_body_shape() -> None:
    error = AppError("TIMEOUT", "受控端在 25 秒内没有响应", retryable=True)
    body = error_payload(error)
    assert {"code", "message", "retryable"} <= set(body)
    assert body["code"] == "TIMEOUT"
    assert body["retryable"] is True
    assert isinstance(body.get("server_time_ms"), int), "错误体必须带服务端时间（A12）"
    assert "detail" not in body, "0.3.0 不再使用 FastAPI 的 detail 形状"


def test_auth_errors_are_structured() -> None:
    for code in ("AUTH", "GONE", "BAD_REQUEST", "UNSUPPORTED", "TIMEOUT", "RATE", "INTERNAL"):
        assert code in {item.value for item in ErrorCode}
    err = AppError.auth("设备令牌无效")
    assert err.code == ErrorCode.AUTH
    assert err.retryable is False
    assert error_payload(err)["code"] == "AUTH"