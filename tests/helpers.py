"""测试共用工具：装配真 app、准备隔离环境、读取仓库文件。

只放“测试侧”逻辑，不 import 被测模块到模块级（W1 期间 server/agent 仍在重写）。
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "server"

#: 测试用固定环境（全部是测试值，不是生产凭据）
TEST_SECRET_KEY = "test-secret-key-0123456789abcdef0123456789"
TEST_ADMIN_PASSWORD = "test-admin-password"
TEST_ENROLL_CODE = "test-enroll-code"
TEST_CONSOLE_HOST = "control.example.test"
TEST_AGENT_HOSTS = "agent.example.test"


def require_server_importable() -> None:
    if str(SERVER_DIR) not in sys.path:
        sys.path.insert(0, str(SERVER_DIR))


def prepare_env(tmp_dir: Path) -> None:
    """把 app 指向临时目录并设置测试凭据；必须在 create_app 之前调用。"""

    require_server_importable()
    os.environ.update(
        {
            "OBS_DATA_DIR": str(tmp_dir),
            "OBS_SECRET_KEY": TEST_SECRET_KEY,
            "OBS_ADMIN_PASSWORD": TEST_ADMIN_PASSWORD,
            "OBS_ENROLL_CODE": TEST_ENROLL_CODE,
            "OBS_CONSOLE_HOST": TEST_CONSOLE_HOST,
            "OBS_AGENT_HOSTS": TEST_AGENT_HOSTS,
        }
    )
    # settings 是 lru_cache 单例；每个用例换 data_dir 前必须清掉
    from observatory.core.config import reset_settings_cache  # noqa: PLC0415

    reset_settings_cache()


@contextmanager
def running_app(tmp_dir: Path) -> Iterator[object]:
    """在临时目录里起一个真 app（lifespan 已启动：DB 迁移、media worker）。

    HostSplit 默认 fail-closed，TestClient 默认 Host=testserver 会被 404；
    这里统一用 loopback（默认允许）作为 Host。
    """

    prepare_env(tmp_dir)
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from observatory.main import create_app  # noqa: PLC0415

    with TestClient(create_app(), headers={"host": "localhost"}) as client:
        yield client


def legacy_token(client: object) -> str:
    """用 0.2.10 真实 enroll 报文换设备令牌（legacy 路由在 0.4 前一直提供）。"""

    from tests.contract.fixtures import agent_0_2_10 as legacy  # noqa: PLC0415

    response = client.post("/api/v1/agent/enroll", json=legacy.enroll_request(code=TEST_ENROLL_CODE))
    assert response.status_code == 200, response.text
    token = response.json().get("token")
    assert isinstance(token, str) and token
    return token


__all__ = [
    "REPO_ROOT",
    "SERVER_DIR",
    "TEST_ADMIN_PASSWORD",
    "TEST_AGENT_HOSTS",
    "TEST_CONSOLE_HOST",
    "TEST_ENROLL_CODE",
    "TEST_SECRET_KEY",
    "legacy_token",
    "prepare_env",
    "running_app",
]