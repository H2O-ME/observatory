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


def enroll_token(client: object, *, uid: str = "OBS-ENROLL-TEST") -> str:
    """走真实安装链路：控制台签发一次性令牌 → v3 enroll → 返回设备令牌。"""

    login = client.post("/api/v1/auth/login", json={"password": TEST_ADMIN_PASSWORD})
    assert login.status_code == 200, login.text
    issued = client.post("/api/v1/install-tokens", json={"ttl_s": 600})
    assert issued.status_code == 200, issued.text
    install_token = issued.json()["data"]["token"]
    payload = {
        "uid": uid,
        "hostname": "enroll-test",
        "os_name": "Windows",
        "os_version": "11",
        "arch": "AMD64",
        "agent_version": "0.3.1",
        "install_token": install_token,
    }
    response = client.post("/api/v1/agent/enroll", json=payload)
    assert response.status_code == 200, response.text
    token = response.json().get("token")
    assert isinstance(token, str) and token
    return token


def seed_device(client: object, *, uid: str = "OBS-V3-TEST", token: str = "v3-test-token") -> int:
    """直接造一行 devices（v3 /agent/link 只认 DB 里的 token_hash，不再接受任意 token）。

    返回 device_id；token_hash 与 console/install.py 使用同一个
    ``device_token_hash(token, settings.secret_key)``。
    """

    from sqlalchemy import select  # noqa: PLC0415

    from observatory.core.clock import now_ms  # noqa: PLC0415
    from observatory.core.crypto import device_token_hash  # noqa: PLC0415
    from observatory.core.models import Device  # noqa: PLC0415

    app = client.app  # type: ignore[attr-defined]
    digest = device_token_hash(token, app.state.settings.secret_key)

    async def insert() -> int:
        async with app.state.db.sessionmaker() as session:
            existing = (await session.execute(select(Device).where(Device.uid == uid))).scalars().first()
            if existing is not None:
                existing.token_hash = digest
                existing.uninstalled_at_ms = None
                await session.commit()
                return int(existing.id)
            row = Device(
                uid=uid,
                name=uid,
                hostname="test-host",
                os_name="Windows",
                os_version="11",
                arch="AMD64",
                agent_version="0.3.0",
                token_hash=digest,
                enrolled_at_ms=now_ms(),
                last_seen_ms=now_ms(),
                settings={},
                sysinfo={},
                note="pytest",
            )
            session.add(row)
            await session.commit()
            return int(row.id)

    return client.portal.call(insert)  # type: ignore[attr-defined]


def seed_agent_package(client: object, version: str = "0.3.0") -> str:
    """造一个真实可下载的 agent 更新包（DB 行 + packages 目录文件），返回 sha256。

    文件内容必须是可解压 zip 的"外观"由下载/更新链路自行决定；这里用固定字节，
    N2 只要求 sha256 与下载字节一致且 offer 非空。
    """

    import hashlib  # noqa: PLC0415
    import zipfile  # noqa: PLC0415
    from io import BytesIO  # noqa: PLC0415

    app = client.app  # type: ignore[attr-defined]
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("observatory-agent.exe", b"fake-agent-binary")
    data = buffer.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    filename = f"observatory-agent-{version}.zip"

    async def save() -> str:
        from observatory.console.updates import PackageService  # noqa: PLC0415

        async with app.state.db.sessionmaker() as session:
            service = PackageService(app.state.settings)
            package = await service.save(session, filename=filename, data=data, notes="pytest")
            await session.commit()
            return str(package.version)

    client.portal.call(save)  # type: ignore[attr-defined]
    return digest


def run_win_smoke(only: str, *, timeout_s: int = 240) -> dict:
    """跑 ``tools/win_smoke.py --json --only <only>`` 并解析末尾 JSON 摘要。

    返回 ``{"results": [...], "exit_code": 0, ...}``；有 FAIL 时先断言失败。
    Windows-only 系统层验收（A1/A2/A17）用真实冒烟结果作证据，不再静态跳过。
    """

    import json  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    script = REPO_ROOT / "tools/win_smoke.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--json", "--only", only],
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    assert completed.returncode == 0, (
        f"win_smoke --only {only!r} 失败（rc={completed.returncode}）：\n"
        f"{completed.stdout[-3000:]}\n{completed.stderr[-2000:]}"
    )
    payload = None
    for line in reversed(completed.stdout.splitlines()):
        candidate = line.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            try:
                payload = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue
    assert payload is not None, f"win_smoke 未输出 JSON 摘要：\n{completed.stdout[-3000:]}"
    return payload


def win_smoke_result(payload: dict, name: str) -> dict:
    for item in payload.get("results", []):
        if item.get("name") == name:
            return item
    raise AssertionError(f"win_smoke 结果缺少检查项 {name!r}；实际 {[i.get('name') for i in payload.get('results', [])]}")


__all__ = [
    "REPO_ROOT",
    "SERVER_DIR",
    "TEST_ADMIN_PASSWORD",
    "TEST_AGENT_HOSTS",
    "TEST_CONSOLE_HOST",
    "TEST_SECRET_KEY",
    "enroll_token",
    "prepare_env",
    "run_win_smoke",
    "running_app",
    "seed_agent_package",
    "seed_device",
    "win_smoke_result",
]