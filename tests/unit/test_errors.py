"""统一错误码（spec §5.5，A8/可观测）：{code,message,retryable} + HTTP 映射。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

EXPECTED_MAPPING = {
    "AUTH": {401, 403},
    "GONE": {410},
    "BAD_REQUEST": {422},
    "UNSUPPORTED": {422},
    "TIMEOUT": {504},
    "RATE": {429},
    "INTERNAL": {500},
}


@pytest.mark.skip(reason="W1: 等待 observatory/core/errors.py")
def test_error_code_http_mapping() -> None:
    from observatory.core.errors import error_status  # noqa: PLC0415

    for code, statuses in EXPECTED_MAPPING.items():
        assert error_status(code) in statuses, code


@pytest.mark.skip(reason="W1: 等待 core/errors.py 的 ApiError 序列化")
def test_error_body_shape() -> None:
    from observatory.core.errors import ApiError  # noqa: PLC0415

    error = ApiError("TIMEOUT", "受控端在 25 秒内没有响应", retryable=True)
    body = error.to_dict()
    assert set(body) == {"code", "message", "retryable"}
    assert body["code"] == "TIMEOUT"
    assert body["retryable"] is True


@pytest.mark.skip(reason="W1: 桌面端不允许中文自然语言作为唯一错误信息（需 code）")
def test_auth_errors_are_structured() -> None:
    from observatory.core.errors import SCHEMA  # noqa: PLC0415

    assert "AUTH" in SCHEMA.all_codes()
    assert "GONE" in SCHEMA.all_codes()