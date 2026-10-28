"""S4 回归（真 app）：超限请求在网络层被拒，业务副作用不发生。

纯 ASGI 夹具（tests/unit/test_bodylimit.py）证明"读到这里就炸"；这里补一条
端到端证据：真的 app + 真的中间件栈（HostSplit / ConsoleRoute / 错误处理）
下，5 MiB 打到限制 256 KiB 的注册路径时返回统一 413 信封，且 devices 表里
不会留下半条注册记录。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

KIB = 1024
MIB = 1024 * KIB
ENROLL_LIMIT = 256 * KIB
UID = "OBS-BODY-LIMIT"


def _enroll_payload() -> bytes:
    return (
        b'{"uid":"' + UID.encode() + b'","hostname":"body-limit",'
        b'"os_name":"Windows","agent_version":"0.3.7"}'
    )


def _oversized_body() -> bytes:
    payload = _enroll_payload()
    assert len(payload) < 5 * MIB
    return payload + b" " * (5 * MIB - len(payload))


def _device_rows(client: Any) -> int:
    from sqlalchemy import func, select  # noqa: PLC0415

    from observatory.core.models import Device  # noqa: PLC0415

    async def count() -> int:
        async with client.app.state.db.sessionmaker() as session:
            return int(
                (await session.execute(select(func.count()).select_from(Device).where(Device.uid == UID))).scalar()
                or 0
            )

    return client.portal.call(count)


def _assert_413(response: Any) -> None:
    assert response.status_code == 413, response.text
    payload = response.json()
    assert payload["code"] == "BAD_REQUEST", payload
    assert payload["message"] == "request body too large"
    assert payload["retryable"] is False
    assert "server_time_ms" in payload


@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
def test_oversized_enroll_has_no_business_side_effect(tmp_dir: Path, chunked: bool) -> None:
    from tests.helpers import running_app  # noqa: PLC0415

    with running_app(tmp_dir) as client:
        body = _oversized_body()
        assert len(body) > ENROLL_LIMIT
        if chunked:
            # 无 Content-Length：httpx 用 chunked 上传，走"读取时立即终止"分支。
            pieces = (body[offset : offset + 64 * KIB] for offset in range(0, len(body), 64 * KIB))
            response = client.post(
                "/api/v1/agent/enroll",
                content=pieces,
                headers={"content-type": "application/json"},
            )
        else:
            response = client.post(
                "/api/v1/agent/enroll",
                content=body,
                headers={"content-type": "application/json"},
            )
        _assert_413(response)
        assert _device_rows(client) == 0, "超限请求不得产生任何注册副作用"


def test_normal_sized_enroll_still_works(tmp_dir: Path) -> None:
    """别把正常注册一起拒掉（防"修超限把功能修没"）。"""

    from tests.helpers import running_app  # noqa: PLC0415

    with running_app(tmp_dir) as client:
        response = client.post(
            "/api/v1/agent/enroll",
            content=_enroll_payload(),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["token"]
        assert _device_rows(client) == 1
