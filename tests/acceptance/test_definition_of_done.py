"""A1–A24 Definition of Done（spec §3.1）。

W0 状态：断言已按规格写死，除 A20（CI 门禁，本波交付）外全部 `skip(reason="W…")`，
等对应波次接口就位后只需去掉装饰器，测试体本身不需要重写。

约定：
- 每个函数名以 `test_aNN_` 开头，编号与规格一一对应；`test_a20_` 现在必须通过。
- 所有 skip 必须带 reason（`test_meta_every_skip_has_reason` 会强制检查）。
- 断言一律针对**行为**（HTTP 状态、总线计数、报文形状），不依赖实现细节命名。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import sys
import time
import uuid
from pathlib import Path

import pytest

# 注意：本模块启用了 `from __future__ import annotations`，函数内定义的
# FastAPI 路由的参数注解会以字符串形式被 FastAPI 在**模块全局**求值——
# WebSocket 必须在模块级导入，否则路由的 websocket 参数会被当成 query
# 参数拒绝（2026-10-25 a23 测试的踩坑）。WebSocketDisconnect 仅个别用例
# 引用，维持函数内局部导入。
from fastapi import WebSocket

pytestmark = pytest.mark.acceptance

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# A1–A24
# ---------------------------------------------------------------------------


@pytest.mark.windows_only
def test_a01_uninstall_helper_onedir_ack_after_alive() -> None:
    """A1：真实 onedir 产物启动 helper 成功；ack 只在 helper 确认存活后发出。"""

    from tests.helpers import run_win_smoke, win_smoke_result

    payload = run_win_smoke("uninstall_staging")
    result = win_smoke_result(payload, "uninstall_staging")
    assert result["status"] == "PASS", f"uninstall_staging 未通过: {result['detail']}"

    source = (REPO_ROOT / "agent/observatory_agent/tasks/uninstall.py").read_text(encoding="utf-8")
    assert source.index("await_confirmation(") < source.index("await ack("), "ack 必须晚于 helper 的 ok=true 确认"
    assert "report_failure" in source, "helper 启动失败必须上报 uninstall_failed，且保持服务运行"
    assert "status_document(ok=False" in source or "status_document(\n        ok=False" in source or "ok=False" in source


@pytest.mark.windows_only
def test_a02_restart_agent_actually_restarts(tmp_dir: Path, caplog) -> None:
    """A2：restart_agent 路径可用（SCM/重启任务 XML），boot_id 变化触发 agent.rebooted。"""

    import logging

    from tests.helpers import run_win_smoke, running_app, seed_device, win_smoke_result

    payload = run_win_smoke("service_readonly")
    result = win_smoke_result(payload, "service_readonly")
    assert result["status"] == "PASS", f"service_readonly 未通过: {result['detail']}"

    from observatory.core.models import Device  # noqa: PLC0415

    assert "boot_id" in Device.__table__.columns, "devices 必须有 boot_id 列（spec §6）"

    def hello(boot_id: str, msg_id: str) -> dict[str, object]:
        return {
            "v": 3,
            "id": msg_id,
            "t": "hello",
            "ts_ms": 1_730_000_000_000,
            "body": {
                "agent_version": "0.3.0",
                "protocol": 3,
                "boot_id": boot_id,
                "device_uid": "OBS-A2",
                "last_ack_id": "",
                "device_time_ms": 1_730_000_000_000,
            },
        }

    with running_app(tmp_dir) as client:
        device_id = seed_device(client, uid="OBS-A2", token="a2-token")
        caplog.set_level(logging.INFO, logger="observatory.link.agent_state")
        # 第一次 hello 是首次登记（previous 为空，不算重启）；换 boot_id 的第二次才是重启。
        first = client.post(
            "/api/v1/agent/link",
            headers={"X-Device-Token": "a2-token"},
            json={"v": 3, "last_ack_id": "", "items": [hello("boot-old", "a02-hello-1")]},
        )
        assert first.status_code == 200, first.text
        response = client.post(
            "/api/v1/agent/link",
            headers={"X-Device-Token": "a2-token"},
            json={"v": 3, "last_ack_id": "", "items": [hello("boot-new", "a02-hello-2")]},
        )
        assert response.status_code == 200, response.text
        assert "agent.rebooted" in caplog.text, "boot_id 变化必须记 agent.rebooted"

        async def read_boot_id() -> str:
            from observatory.core.models import Device as Model  # noqa: PLC0415

            async with client.app.state.db.sessionmaker() as session:
                device = await session.get(Model, device_id)
                return device.boot_id

        assert client.portal.call(read_boot_id) == "boot-new", "hello 的 boot_id 必须落库"


def test_a03_update_requires_sha256_and_aborts_on_mismatch(tmp_dir: Path) -> None:
    """A3：无摘要/错摘要/不存在的包的 update 命令被拒（422）；摘要不符时中止且不替换。"""

    import sys as _sys

    from tests.helpers import TEST_ADMIN_PASSWORD, running_app, seed_agent_package, seed_device

    # 1) 服务端：update 命令必须有合法 sha256、且与包库中该版本一致（0.3.1 F5）
    with running_app(tmp_dir) as client:
        assert client.post("/api/v1/auth/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
        seed_device(client, uid="OBS-A3", token="a3-token")
        missing = client.post(
            "/api/v1/devices/OBS-A3/commands",
            json={"type": "update", "payload": {"version": "0.3.0"}},
        )
        assert missing.status_code == 422, missing.text
        assert missing.json().get("code") == "BAD_REQUEST"
        invalid = client.post(
            "/api/v1/devices/OBS-A3/commands",
            json={"type": "update", "payload": {"version": "0.3.0", "sha256": "not-a-digest"}},
        )
        assert invalid.status_code == 422
        # 包不存在：即使 sha256 格式合法也拒绝（F5）
        unknown = client.post(
            "/api/v1/devices/OBS-A3/commands",
            json={"type": "update", "payload": {"version": "0.3.0", "sha256": "a" * 64}},
        )
        assert unknown.status_code == 422, unknown.text
        assert "安装包" in unknown.json()["message"]
        # 包存在但 sha256 与包不符：拒绝
        real_sha = seed_agent_package(client, version="0.3.0")
        mismatch = client.post(
            "/api/v1/devices/OBS-A3/commands",
            json={"type": "update", "payload": {"version": "0.3.0", "sha256": "a" * 64}},
        )
        assert mismatch.status_code == 422, mismatch.text
        assert "sha256" in mismatch.json()["message"]
        ok = client.post(
            "/api/v1/devices/OBS-A3/commands",
            json={"type": "update", "payload": {"version": "0.3.0", "sha256": real_sha}},
        )
        assert ok.status_code == 200, ok.text
        # F7：响应带 deadline_ms
        assert isinstance(ok.json().get("deadline_ms"), int) and ok.json()["deadline_ms"] > 0

    # 2) 受控端：空/非法摘要直接拒绝；摘要不符时中止（不进入替换步骤）
    _sys.path.insert(0, str(REPO_ROOT / "agent"))
    from observatory_agent.tasks.update import (  # noqa: PLC0415
        UpdateFailed,
        UpdateRejected,
        validate_sha256,
        verify_package,
    )

    with pytest.raises(UpdateRejected):
        validate_sha256("")
    with pytest.raises(UpdateRejected):
        validate_sha256("xyz")
    package = tmp_dir / "observatory-agent-0.3.0.zip"
    package.write_bytes(b"not the expected bytes")
    install_dir = tmp_dir / "install"
    install_dir.mkdir()
    (install_dir / "marker.txt").write_text("old-install", encoding="utf-8")
    with pytest.raises((UpdateFailed, UpdateRejected)):
        verify_package(package, "0" * 64)
    assert (install_dir / "marker.txt").read_text(encoding="utf-8") == "old-install", (
        "摘要不符只能中止，不能替换已安装目录"
    )


def test_a04_secrets_never_reach_agent_or_devices_api(tmp_dir: Path) -> None:
    """A4：下发给受控端的任何 config JSON 与 /devices 响应都不含 SECRET_KEYS。"""

    from observatory.console.dto import device_safe_config
    from observatory.core.settings_spec import AGENT_VISIBLE_KEYS, SECRET_KEYS
    from tests.helpers import TEST_ADMIN_PASSWORD, enroll_token, running_app

    # 1) 单一投影：secret 键被剥离，agent 可见集与 secret 集不相交
    assert not (AGENT_VISIBLE_KEYS & SECRET_KEYS)
    assert "archive_token" in SECRET_KEYS
    projected = device_safe_config({**{key: "dummy-secret" for key in SECRET_KEYS}, "capture_interval_s": 30})
    assert not (set(projected) & SECRET_KEYS)
    assert projected["capture_interval_s"] == 30

    with running_app(tmp_dir) as client:
        login = client.post("/api/v1/auth/login", json={"password": TEST_ADMIN_PASSWORD})
        assert login.status_code == 200, login.text
        # 写入真实 secret，证明过滤不是"恰好为空"
        patched = client.patch(
            "/api/v1/settings",
            json={
                "values": {
                    "archive_token": "ghp_very_secret_token",
                    "archive_enabled": True,
                    "archive_repo": "owner/repo",
                }
            },
        )
        assert patched.status_code == 200, patched.text

        token = enroll_token(client)
        assert token, "enroll 应返回设备令牌"
        # v3 链路的 config 出口是 enroll 响应与 welcome.config；enroll 响应即投影结果
        devices = client.get("/api/v1/devices").json()["data"]["devices"]
        assert devices, "至少应有一台已注册设备"
        serialized = json.dumps(devices, ensure_ascii=False)
        assert "ghp_very_secret_token" not in serialized
        assert "archive_token" not in serialized
        for device in devices:
            assert set(device["settings"]) <= AGENT_VISIBLE_KEYS

        effective = client.get(f"/api/v1/settings/effective/{devices[0]['uid']}")
        assert effective.status_code == 200
        assert "archive_token" not in json.dumps(effective.json(), ensure_ascii=False)

    # 2) 静态：所有出站 config 路径都走同一个投影函数
    server = REPO_ROOT / "server/observatory"
    for relative in ("console/settings.py", "console/devices.py", "console/enroll.py"):
        assert "device_safe_config" in (server / relative).read_text(encoding="utf-8"), relative


def test_a05_revoke_stops_ctrl_in_and_ttl_closes(tmp_dir: Path) -> None:
    """A5：revoke 后 ctrl_in 不再转发；TTL 到期自动 ctrl_stop 并关 WS（4408）。"""

    from fastapi import WebSocketDisconnect  # noqa: PLC0415

    from tests.helpers import TEST_ADMIN_PASSWORD, running_app, seed_device  # noqa: PLC0415

    def hello(msg_id: str) -> dict[str, object]:
        return {
            "v": 3,
            "id": msg_id,
            "t": "hello",
            "ts_ms": 1_730_000_000_000,
            "body": {
                "agent_version": "0.3.0",
                "protocol": 3,
                "boot_id": "boot-a5",
                "device_uid": "OBS-A5",
                "last_ack_id": "",
                "device_time_ms": 1_730_000_000_000,
            },
        }

    with running_app(tmp_dir) as client:
        seed_device(client, uid="OBS-A5", token="a5-token")
        login = client.post("/api/v1/auth/login", json={"password": TEST_ADMIN_PASSWORD})
        assert login.status_code == 200, login.text

        def create_grant() -> str:
            response = client.post(
                "/api/v1/devices/OBS-A5/control",
                json={"confirm": True, "ttl_s": 30},
            )
            assert response.status_code == 200, response.text
            token = response.json()["data"]["token"]
            assert isinstance(token, str) and token
            return token

        acked: dict[str, str] = {"last": ""}

        def heartbeat(msg_id: str) -> dict[str, object]:
            return {
                "v": 3,
                "id": msg_id,
                "t": "heartbeat",
                "ts_ms": 1_730_000_000_100,
                "body": {
                    "agent_version": "0.3.0",
                    "boot_id": "boot-a5",
                    "state": {"transport": "ws"},
                    "last_ack_id": acked["last"],
                },
            }

        seen: set[str] = set()

        def next_control(agent_ws: object, action: str) -> dict:
            """读下一条 control 信封并用 last_ack_id 消费（outbox at-least-once）。

            已消费 id 的重复投递直接跳过——与真实受控端按下行 id 去重一致。
            """

            while True:
                message = agent_ws.receive_json()
                kind = message.get("t")
                if kind == "pong" or message.get("id") in seen:
                    continue
                assert kind == "control" and message["body"].get("action") == action, message
                seen.add(message["id"])
                acked["last"] = message["id"]
                agent_ws.send_json(heartbeat(f"a5-hb-{uuid.uuid4().hex[:8]}"))
                return message

        # ---- 路径 1：主动撤销（DELETE）→ ctrl_stop + 关闭 4410 ----
        with client.websocket_connect(
            "/api/v1/agent/link", headers={"X-Device-Token": "a5-token"}
        ) as agent_ws:
            agent_ws.send_json(hello("a5-hello"))
            welcome = agent_ws.receive_json()
            assert welcome["t"] == "welcome", welcome

            token = create_grant()
            # 路径 1 只测 revoke 语义：CI 慢机（2 CPU + portal 往返）上从 grant()
            # 到 DELETE 可能跨过真实 30s TTL，grant 会被 TTL 定时器先收走（0.3.1
            # CI 实录）。主动延长有效期，把"到期自动停止"留给路径 2 的定时器断言。
            from observatory.console.control import SESSIONS as _A05_SESSIONS
            from observatory.core.clock import now_ms as _a05_now

            _grant_a5 = _A05_SESSIONS.get(token)
            assert _grant_a5 is not None
            _grant_a5.expires_at_ms = _a05_now() + 300_000
            with client.websocket_connect(f"/api/v1/devices/OBS-A5/control?token={token}") as viewer:
                ready = viewer.receive_json()
                assert ready["type"] == "ready", ready
                next_control(agent_ws, "start")

                # 授权期间输入正常转发
                viewer.send_json({"type": "input", "ev": {"kind": "move", "x": 10, "y": 20}})
                next_control(agent_ws, "input")

                revoked = client.delete(f"/api/v1/devices/OBS-A5/control/{token}")
                assert revoked.status_code == 200, revoked.text

                next_control(agent_ws, "stop")
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    while True:
                        viewer.receive_json()
                assert exc_info.value.code == 4410, f"撤销必须关闭 4410: {exc_info.value.code}"

            # 撤销后再连同一 token 必须被拒（授权已失效，输入无从转发）
            with pytest.raises(WebSocketDisconnect) as rejected:
                with client.websocket_connect(f"/api/v1/devices/OBS-A5/control?token={token}") as viewer:
                    while True:
                        viewer.receive_json()
            assert rejected.value.code in {4401, 4410}, rejected.value.code

        # ---- 路径 2：TTL 到期 → ctrl_stop + 关闭 4408（定时器路径）----
        from observatory.console.control import SESSIONS  # noqa: PLC0415
        from observatory.core.clock import now_ms  # noqa: PLC0415

        seen.clear()
        acked["last"] = ""
        with client.websocket_connect(
            "/api/v1/agent/link", headers={"X-Device-Token": "a5-token"}
        ) as agent_ws:
            agent_ws.send_json(hello("a5-hello-2"))
            assert agent_ws.receive_json()["t"] == "welcome"
            token = create_grant()
            # 定时器在 viewer 接入时才启动，先收短 TTL 再接入即无需等满 30s
            grant = SESSIONS.get(token)
            assert grant is not None
            grant.expires_at_ms = now_ms() + 600
            with client.websocket_connect(f"/api/v1/devices/OBS-A5/control?token={token}") as viewer:
                assert viewer.receive_json()["type"] == "ready"
                next_control(agent_ws, "start")
                next_control(agent_ws, "stop")
                with pytest.raises(WebSocketDisconnect) as ttl_closed:
                    while True:
                        viewer.receive_json()
                assert ttl_closed.value.code == 4408, f"TTL 必须关闭 4408: {ttl_closed.value.code}"


def _deploy_text(name: str) -> str:
    path = REPO_ROOT / "deploy" / name
    assert path.exists(), f"缺少 {path.relative_to(REPO_ROOT)}"
    return path.read_text(encoding="utf-8")


def test_a06_certificates_are_real_and_renewal_fails_loudly() -> None:
    """A6：部署产出实体证书（install 拷贝，非悬空符号链接）；续期失败退出非 0。"""

    setup = _deploy_text("setup.sh")
    renew = _deploy_text("renew-certs.sh")

    # 首次申请：单张双域名证书（webroot），两个域名都做 SAN 断言 + 有效期断言
    assert "certbot certonly --webroot" in setup
    assert 'for host in "$CONSOLE_HOST" "$AGENT_HOST"' in setup
    assert '-checkhost "$host"' in setup
    assert "-checkend 86400" in setup

    # 实体拷贝：install 到 APP_DIR/certs（不是绝对符号链接，容器内才不会指向自身）
    assert 'install -m 0644 "$cert_dir/fullchain.pem" "$APP_DIR/certs/server.crt"' in setup
    assert 'install -m 0600 "$cert_dir/privkey.pem" "$APP_DIR/certs/server.key"' in setup
    assert 'chown "$OBS_UID:$OBS_UID"' in setup
    code_lines = "\n".join(
        line for line in setup.splitlines() if not line.lstrip().startswith("#")
    )
    assert not re.search(
        r"ln\s+-s\w*\s+[^\n]*(?:server\.(?:crt|key)|fullchain\.pem|privkey\.pem)", code_lines
    ), "证书不能用符号链接交付（容器只挂 certs/）"

    # 续期：webroot + 失败 die → exit 1；成功后仍实体拷贝并重载/重启
    assert "set -Eeuo pipefail" in renew
    assert "certbot renew --cert-name" in renew
    assert 'die "certbot renew 失败' in renew
    assert "exit 1" in renew
    assert "copy_certs" in renew
    assert 'install -m 0644 "$CERT_DIR/fullchain.pem" "$APP_DIR/certs/server.crt"' in renew
    assert 'install -m 0600 "$CERT_DIR/privkey.pem" "$APP_DIR/certs/server.key"' in renew
    assert renew.rstrip().endswith("exit 0"), "续期脚本成功路径必须显式 exit 0"


def test_a07_upgrade_backs_up_and_rolls_back() -> None:
    """A7：升级前备份 DB + 记录 commit/镜像；健康失败自动回退且退出非 0。"""

    update = _deploy_text("update.sh")
    backup = _deploy_text("backup.sh")

    # 升级前：记录旧 commit/版本/镜像，并调用 backup.sh；备份失败中止
    assert "record_upgrade_state" in update
    assert "OBS_UPDATE_ROLLBACK_COMMIT" in update
    assert "OBS_UPDATE_ROLLBACK_VERSION" in update
    assert "OBS_UPDATE_ROLLBACK_IMAGE" in update
    assert "upgrade.env" in update
    assert 'bash "$APP_DIR/deploy/backup.sh"' in update
    assert "backup_before_upgrade" in update
    assert "SKIP_BACKUP" in update

    # 备份必须发生在拉代码/换镜像之前（失败则什么都不改）
    main_body = update[update.index("main() {"):]
    assert main_body.index("backup_before_upgrade") < main_body.index("git fetch --prune origin")

    # 失败回滚：reset 代码 + 旧镜像启动 + 健康检查失败 exit 1
    assert "rollback()" in update
    assert ' -C "$APP_DIR" reset --hard "$OLD_COMMIT"' in update
    assert "compose up -d --no-build" in update
    assert "if ! wait_health 60; then" in update
    assert 'rollback "健康检查' in update
    assert 'rollback "镜像构建失败"' in update
    assert 'rollback "容器启动失败"' in update
    assert "trap on_exit EXIT" in update
    assert re.search(r"^\s*exit 1$", update, re.MULTILINE)

    # 备份脚本本身：SQLite backup API（一致性快照）+ 数据卷 tar
    assert "sqlite3" in backup and "source.backup(target)" in backup
    assert "tar czf" in backup


def test_a08_deadline_never_inverts_and_slow_device_succeeds() -> None:
    """A8：所有接口服务端 deadline < 客户端超时；一次写死，不许各接口自定。

    "25s 慢设备" 的可运行版本在 W2 用可注入延迟的假受控端覆盖；这里钉死预算表、
    响应契约（deadline_ms 是相对毫秒）以及 session 的唯一取值来源。
    """

    from observatory.console.dto import with_deadline
    from observatory.core.clock import CLIENT_TIMEOUT_GRACE_MS, client_timeout_s, server_deadline_ms
    from observatory.core.config import Settings

    cfg = Settings()
    for kind in ("files", "update", "uninstall", "sysinfo", "screenshot", "log_tail"):
        deadline = server_deadline_ms(kind, cfg)
        client_timeout_ms = client_timeout_s(kind, cfg) * 1000
        assert deadline <= 25_000
        assert client_timeout_ms == deadline + CLIENT_TIMEOUT_GRACE_MS
        assert client_timeout_ms > deadline, f"{kind}: 客户端超时必须大于服务端 deadline"

    stamped = with_deadline({"ok": True}, "files")
    assert stamped["deadline_ms"] == server_deadline_ms("files", cfg)
    assert 0 < stamped["deadline_ms"] < 10**12, "deadline_ms 是相对预算而不是 epoch 时间戳"

    session_src = (REPO_ROOT / "server/observatory/link/session.py").read_text(encoding="utf-8")
    assert "server_deadline_ms" in session_src and "client_timeout_s" in session_src
    assert "timeout_s=20" not in session_src, "禁止接口各自写死 20s"


def test_a09_no_waiter_leak_after_1000_commands(tmp_dir: Path) -> None:
    """A9：连续创建 1000 条不等待的命令后，等待者条目数回到 0（含取消路径）。"""

    from sqlalchemy import select

    from observatory.core.clock import now_ms
    from observatory.core.models import Command, Device
    from tests.helpers import enroll_token, running_app

    with running_app(tmp_dir) as client:
        enroll_token(client)
        app = client.app

        async def first_device_id() -> int:
            async with app.state.db.sessionmaker() as session:
                row = (await session.execute(select(Device))).scalars().first()
                assert row is not None
                return int(row.id)

        device_id = client.portal.call(first_device_id)

        async def seed_commands() -> list[str]:
            now = now_ms()
            ids: list[str] = []
            async with app.state.db.sessionmaker() as session:
                for index in range(1000):
                    command_id = str(uuid.uuid4())
                    session.add(
                        Command(
                            id=command_id,
                            device_id=device_id,
                            type="sysinfo",
                            payload={"seq": index},
                            status="pending",
                            actor="console",
                            created_at_ms=now,
                            expires_at_ms=now + 60_000,
                            deadline_ms=20_000,
                            attempts=0,
                        )
                    )
                    ids.append(command_id)
                await session.commit()
            return ids

        command_ids = client.portal.call(seed_commands)
        assert len(command_ids) == 1000

        async def enter_1000_scopes() -> tuple[int, int]:
            journal = app.state.journal
            assert journal.open_count() == 0
            for command_id in command_ids:
                async with journal.expect(command_id):
                    assert journal.open_count() >= 1
            return journal.open_count(), len(command_ids)

        after, count = client.portal.call(enter_1000_scopes)
        assert count == 1000
        assert after == 0, "1000 个 scoped waiter 退出后必须归零"

        async def cancelled_waiter_leaves_nothing() -> int:
            journal = app.state.journal
            command_id = command_ids[0]

            async def waiter() -> None:
                async with journal.expect(command_id) as slot:
                    await slot.wait(30)

            task = asyncio.create_task(waiter())
            await asyncio.sleep(0)
            assert journal.open_count() == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return journal.open_count()

        assert client.portal.call(cancelled_waiter_leaves_nothing) == 0


def test_a10_command_delivery_closure_after_reconnect(tmp_dir: Path) -> None:
    """A10：WS 断开期积压命令重连后 ≤1 心跳送达；回执丢失重投不重复执行。

    附带 §5.6 的 ack 语义（方向相反，容易写反）：
    - 发 3 条上行 a,b,c → 响应 `ack_id == c`（服务端已处理的上行 id）；
    - 再发 `last_ack_id=<下行 id>` → 服务端不再重发该下行。
    """

    from tests.helpers import running_app, seed_device

    with running_app(tmp_dir) as client:
        device_id = seed_device(client, uid="OBS-A10", token="a10-token")

        async def create_pending_command() -> str:
            record = await client.app.state.journal.create(device_id, "sysinfo", {"seq": 1})
            return record.id

        command_id = client.portal.call(create_pending_command)

        hello = {
            "v": 3,
            "id": "a10-hello",
            "t": "hello",
            "ts_ms": 1_730_000_000_000,
            "body": {
                "agent_version": "0.3.0",
                "protocol": 3,
                "boot_id": "a10-boot",
                "device_uid": "OBS-A10",
                "last_ack_id": "",
                "device_time_ms": 1_730_000_000_000,
            },
        }
        # 离线期积压的命令在重连后的第一个响应里补投，command_id 保持不变
        reconnected = client.post(
            "/api/v1/agent/link",
            headers={"X-Device-Token": "a10-token"},
            json={"v": 3, "last_ack_id": "", "items": [hello]},
        )
        assert reconnected.status_code == 200, reconnected.text
        delivered = [
            item for item in reconnected.json()["items"] if item["t"] == "command"
        ]
        assert delivered, "重连后必须补投离线期积压的命令"
        assert delivered[0]["body"]["command_id"] == command_id, "补投必须复用稳定 command_id"

        # 回执丢失场景：同一 command_id 的 result 重投是幂等的
        result_item = {
            "v": 3,
            "id": "a10-result-1",
            "t": "result",
            "ts_ms": 1_730_000_000_100,
            "body": {"command_id": command_id, "status": "ok", "result": {"ok": True}},
        }
        reported = client.post(
            "/api/v1/agent/link",
            headers={"X-Device-Token": "a10-token"},
            json={"v": 3, "last_ack_id": "", "items": [result_item]},
        )
        assert reported.status_code == 200
        assert reported.json()["ack_id"] == "a10-result-1"

        # 已完成命令不会再次补投
        after = client.post(
            "/api/v1/agent/link",
            headers={"X-Device-Token": "a10-token"},
            json={"v": 3, "last_ack_id": "a10-result-1", "items": []},
        )
        assert after.status_code == 200
        assert all(item["body"].get("command_id") != command_id for item in after.json()["items"]), (
            "已回执的命令不能重复补投（A10 无重复执行）"
        )

        # ack 游标：批量上行 a,b,c → ack_id 是最后处理的上行 id c
        abc = [
            {
                "v": 3,
                "id": uplink_id,
                "t": "event",
                "ts_ms": 1_730_000_000_200,
                "body": {"event": "noop", "data": {"seq": index}},
            }
            for index, uplink_id in enumerate(("a", "b", "c"))
        ]
        batch = client.post(
            "/api/v1/agent/link",
            headers={"X-Device-Token": "a10-token"},
            json={"v": 3, "last_ack_id": "a10-result-1", "items": abc},
        )
        assert batch.status_code == 200
        assert batch.json()["ack_id"] == "c", "ack_id 必须推进到最后一个上行 id"


def test_a11_default_bind_and_forwarded_headers_and_hostsplit() -> None:
    """A11：默认 127.0.0.1、trust_forwarded_for=false、HostSplit 配置不全 fail-closed。"""

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from observatory.adapters.hostsplit import HostSplit
    from observatory.core.config import Settings

    inner = FastAPI()

    @inner.get("/api/v1/health")
    def health() -> dict:
        return {"ok": True}

    @inner.get("/console")
    def console() -> dict:
        return {"page": "console"}

    def app_client(**kwargs: object) -> TestClient:
        app = HostSplit(
            inner,
            console_hosts=kwargs.get("console_hosts", []),  # type: ignore[arg-type]
            agent_hosts=kwargs.get("agent_hosts", []),  # type: ignore[arg-type]
            trust_forwarded_for=bool(kwargs.get("trust_forwarded_for", False)),
        )
        return TestClient(app)

    # 1) 配置不全 → fail-closed：未知 Host 一律 404，只放行回环
    incomplete = app_client()
    assert incomplete.get("/console", headers={"host": "evil.example"}).status_code == 404
    assert incomplete.get("/api/v1/health", headers={"host": "evil.example"}).status_code == 404
    assert incomplete.get("/console", headers={"host": "localhost"}).status_code == 200
    assert incomplete.get("/api/v1/health", headers={"host": "localhost"}).status_code == 200

    # 2) 完整配置：控制台只认 console host；agent host 只能访问共享路径
    complete = app_client(console_hosts=["console.test"], agent_hosts=["agent.test"])
    assert complete.get("/console", headers={"host": "console.test"}).status_code == 200
    assert complete.get("/console", headers={"host": "agent.test"}).status_code == 404
    assert complete.get("/api/v1/health", headers={"host": "agent.test"}).status_code == 200
    assert complete.get("/api/v1/health", headers={"host": "unknown.test"}).status_code == 404

    # 3) trust_forwarded_for 默认 false：X-Forwarded-Host 不能伪造控制台 host
    spoofed = {"host": "evil.example", "x-forwarded-host": "console.test"}
    assert complete.get("/console", headers=spoofed).status_code == 404
    trusting = app_client(console_hosts=["console.test"], agent_hosts=["agent.test"], trust_forwarded_for=True)
    assert trusting.get("/console", headers=spoofed).status_code == 200

    # 4) 默认设置与容器端口绑定
    defaults = Settings()
    assert defaults.trust_forwarded_for is False
    compose = (REPO_ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    assert "${OBS_BIND_ADDR:-127.0.0.1}" in compose, "默认必须只绑回环（A11）"


def test_a23_console_websockets_served_on_agent_host() -> None:
    """A23 补充（2026-10-25 生产）：控制台域名可能挂在不支持 WebSocket 的
    CDN（阿里云 ESA）后面——控制台的两条 WS（实时事件流 /api/v1/ws、实时
    控制观看端 /api/v1/devices/<uid>/control）必须能在 agent 域名承载；
    同路径的 REST 仍只归 console 域。"""

    from fastapi import FastAPI, WebSocketDisconnect
    from fastapi.testclient import TestClient

    from observatory.adapters.hostsplit import HostSplit

    inner = FastAPI()

    @inner.websocket("/api/v1/devices/{uid}/control")
    async def control_ws(websocket: WebSocket, uid: str) -> None:  # noqa: ARG001
        await websocket.accept()
        await websocket.send_json({"type": "ready"})
        await websocket.close()

    @inner.websocket("/api/v1/ws")
    async def events_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"type": "hello"})
        await websocket.close()

    @inner.post("/api/v1/devices/{uid}/control")
    async def control_grant(uid: str) -> dict:  # noqa: ARG001
        return {"ok": True}

    client = TestClient(
        HostSplit(inner, console_hosts=["console.test"], agent_hosts=["agent.test"])
    )

    # agent host：两条 WS 可建立（经 direct/agent 域承载控制台实时通道）
    with client.websocket_connect(
        "/api/v1/devices/OBS-X/control", headers={"host": "agent.test"}
    ) as viewer:
        assert viewer.receive_json() == {"type": "ready"}
    with client.websocket_connect("/api/v1/ws", headers={"host": "agent.test"}) as events:
        assert events.receive_json() == {"type": "hello"}

    # console host 两条 WS 照常
    with client.websocket_connect(
        "/api/v1/devices/OBS-X/control", headers={"host": "console.test"}
    ) as viewer:
        assert viewer.receive_json() == {"type": "ready"}

    # 同路径 REST 仍只归 console host；未知 host 的 WS 一律拒绝
    assert client.post("/api/v1/devices/OBS-X/control", headers={"host": "agent.test"}).status_code == 404
    assert client.post("/api/v1/devices/OBS-X/control", headers={"host": "console.test"}).status_code == 200
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/v1/devices/OBS-X/control", headers={"host": "evil.test"}
        ):
            pass


def test_a12_single_clock_source(tmp_dir: Path) -> None:
    """A12：FileListOut.server_time_ms 来自服务端；前端无 Date.now 参与新鲜度。"""

    from observatory.console.dto import file_entry_out, stamp
    from observatory.core.clock import now_ms
    from tests.helpers import TEST_ADMIN_PASSWORD, enroll_token, running_app

    # 1) DTO 层：server_time_ms 由服务端时钟注入；设备时间改名 device_time_ms
    before = now_ms()
    stamped = stamp({"ok": True})
    after = now_ms()
    assert before <= stamped["server_time_ms"] <= after
    entry = file_entry_out({"name": "a.txt", "path": "C:/a.txt", "mtime_ms": 123})
    assert entry["device_time_ms"] == 123
    assert "server_time_ms" not in entry, "受控端时间不能叫 server_time_ms"

    # 2) 真 app：设备列表的 server_time_ms 来自服务端（且 presence 内层同样是服务端时钟）
    with running_app(tmp_dir) as client:
        login = client.post("/api/v1/auth/login", json={"password": TEST_ADMIN_PASSWORD})
        assert login.status_code == 200
        enroll_token(client)
        listed = client.get("/api/v1/devices")
        assert listed.status_code == 200
        body = listed.json()
        assert isinstance(body["server_time_ms"], int)
        assert abs(body["server_time_ms"] - now_ms()) < 10_000
        device = body["data"]["devices"][0]
        assert isinstance(device["server_time_ms"], int)
        assert isinstance(device["presence"]["server_time_ms"], int)

    # 3) 文件 REST 路径统一走 stamp/file_entry_out（静态）
    files_src = (REPO_ROOT / "server/observatory/console/files.py").read_text(encoding="utf-8")
    assert "stamp(" in files_src and "file_entry_out" in files_src
    assert "server_time_ms=" not in files_src

    # 4) 前端：时钟基于 performance.now()，代码里没有 Date.now() 兜底
    webui = REPO_ROOT / "webui/src"
    clock = re.sub(r"/\*.*?\*/", "", (webui / "lib/clock.ts").read_text(encoding="utf-8"), flags=re.DOTALL)
    assert "performance.now()" in clock
    assert "Date.now(" not in clock
    for path in webui.rglob("*"):
        if path.suffix not in {".ts", ".tsx"}:
            continue
        code = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        code = re.sub(r"(?m)^\s*//.*$", "", code)
        assert "Date.now(" not in code, f"{path.relative_to(REPO_ROOT)} 仍用 Date.now() 判新鲜度"


def test_a13_capabilities_are_exact() -> None:
    """A13：control_enabled=false → control=false；无包 → update=false；无通道 → files=false。"""

    from tests.unit.test_presence_matrix import NOW, FakeSession, _context, _device

    context = _context({1: FakeSession("ws", NOW - 1_000)})
    online = context.snapshot(_device())
    assert online["capabilities"] == {
        "control": True,
        "files": True,
        "screenshot": True,
        "timeline": True,
        "update": False,
        "uninstall": True,
    }

    disabled = context.snapshot(_device(settings={"control_enabled": False}))
    assert disabled["capabilities"]["control"] is False

    context.latest_agent_version = "0.3.1"
    assert context.snapshot(_device(agent_version="0.3.0"))["capabilities"]["update"] is True
    assert context.snapshot(_device(agent_version="0.2.4"))["capabilities"]["update"] is False

    # 无活动通道 → files/control/uninstall 全 false（degraded）
    degraded = _context().snapshot(_device(last_seen_ms=NOW - 30_000))
    assert degraded["state"] == "degraded"
    assert degraded["capabilities"]["files"] is False
    assert degraded["capabilities"]["control"] is False

    # 单一推导函数：capabilities 只在 presence.py 里计算
    server = REPO_ROOT / "server/observatory"
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in server.rglob("*.py")
        if "presence" not in path.name
        and "adapters" not in path.parts
        and "def _capabilities" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"capabilities 出现第二处推导: {offenders}"


def test_a14_archive_closure(tmp_dir: Path) -> None:
    """A14：上传成功才删本地；删除设备递归删远端；失败进重试队列且可观测。"""

    from tests.integration.test_archive_closure import test_a14_archive_closure as archive_closure

    archive_closure(tmp_dir)


def test_a15_every_setting_key_has_a_consumer() -> None:
    """A15：SPECS 每键都有消费者测试；agent_offline_after_s 真的改变 offline 阈值。"""

    from observatory.core.config import Settings
    from observatory.core.settings_spec import SPECS

    from tests.unit.test_settings_consumers import PENDING_CONSUMERS, _consumed_keys

    missing = set(SPECS) - _consumed_keys()
    assert missing == set(), f"仍有配置项没有消费者: {sorted(missing)}（登记 {PENDING_CONSUMERS}）"
    assert PENDING_CONSUMERS == {}, "消费者已清零，已知清单必须为空集"

    assert Settings(agent_offline_after_s=42).offline_after_ms == 42_000
    assert Settings(agent_offline_after_s=120).offline_after_ms == 120_000


def test_a16_multimonitor_coordinates_within_2px() -> None:
    """A16：双屏负坐标 / DPI150% 点击误差 ≤2px（纯函数）。"""

    from tests.unit import test_coords

    test_coords.test_negative_origin_second_monitor()
    test_coords.test_four_monitors_negative_origin_dpi_150()
    test_coords.test_dpi_150_percent_error_within_2px()
    test_coords.test_normalized_roundtrip_is_lossless_enough()


@pytest.mark.windows_only
def test_a17_pipe_acl_and_401_reregistration(tmp_dir: Path) -> None:
    """A17：管道 ACL 仅 SYSTEM/Administrators/当前用户；401 后自动重注册回主链路。"""

    from tests.helpers import run_win_smoke, running_app, seed_device, win_smoke_result

    # 1) 真实 Windows 管道验证：DACL 成员、客户端身份校验、双向帧（win_smoke 本机实跑）
    for check_name in ("pipe_acl", "pipe_identity", "pipe_duplex"):
        result = win_smoke_result(run_win_smoke(check_name), check_name)
        assert result["status"] == "PASS", f"{check_name} 未通过: {result['detail']}"

    # 2) 受控端 401 语义：HTTP 401/WS 4401 → LinkAuthError → on_unauthorized 重注册
    from websockets.exceptions import ConnectionClosed  # noqa: PLC0415
    from websockets.frames import Close  # noqa: PLC0415

    from observatory_agent.core.link import (  # noqa: PLC0415
        WS_CLOSE_RATE_LIMITED,
        WS_CLOSE_UNAUTHORIZED,
        LinkAuthError,
        LinkGoneError,
        LinkRateLimited,
        _map_ws_error,
    )

    assert WS_CLOSE_UNAUTHORIZED == 4401
    assert isinstance(_map_ws_error(ConnectionClosed(rcvd=Close(4401, "x"), sent=None)), LinkAuthError)
    assert isinstance(_map_ws_error(ConnectionClosed(rcvd=Close(4410, "x"), sent=None)), LinkGoneError)
    assert isinstance(_map_ws_error(ConnectionClosed(rcvd=Close(WS_CLOSE_RATE_LIMITED, "x"), sent=None)), LinkRateLimited)
    runtime_source = (REPO_ROOT / "agent/observatory_agent/core/runtime.py").read_text(encoding="utf-8")
    assert "on_unauthorized=self._reauth" in runtime_source, "Link 必须挂 _reauth 作为 401 重注册回调"

    # 3) 服务端半边：失效 token 401；重注册拿到的同设备新 token 立即可用
    hello = {
        "v": 3,
        "id": "a17-hello",
        "t": "hello",
        "ts_ms": 1_730_000_000_000,
        "body": {
            "agent_version": "0.3.0",
            "protocol": 3,
            "boot_id": "boot-a17",
            "device_uid": "OBS-A17",
            "last_ack_id": "",
            "device_time_ms": 1_730_000_000_000,
        },
    }
    batch = {"v": 3, "last_ack_id": "", "items": [hello]}
    with running_app(tmp_dir) as client:
        seed_device(client, uid="OBS-A17", token="a17-token")
        rejected = client.post("/api/v1/agent/link", headers={"X-Device-Token": "stale-token"}, json=batch)
        assert rejected.status_code == 401, rejected.text
        recovered = client.post("/api/v1/agent/link", headers={"X-Device-Token": "a17-token"}, json=batch)
        assert recovered.status_code == 200, recovered.text


def test_a18_install_token_single_use_ttl_bound(tmp_dir: Path) -> None:
    """A18：安装令牌一次性 + TTL + 绑定首台设备；脚本只嵌入一次性令牌。"""

    from tests.helpers import TEST_ADMIN_PASSWORD, running_app

    with running_app(tmp_dir) as client:
        login = client.post("/api/v1/auth/login", json={"password": TEST_ADMIN_PASSWORD})
        assert login.status_code == 200, login.text

        created = client.post("/api/v1/install-tokens", json={"ttl_s": 120})
        assert created.status_code == 200, created.text
        issued = created.json()["data"]
        token = issued["token"]
        assert isinstance(token, str) and len(token) >= 32
        assert issued["max_uses"] == 1

        # TTL 被服务端限制在 60..86400 秒（越界拒绝，而不是静默截断）
        ttl_left_ms = int(issued["expires_at_ms"]) - int(time.time() * 1000)
        assert 60_000 <= ttl_left_ms <= 120_000 + 5_000, ttl_left_ms
        too_short = client.post("/api/v1/install-tokens", json={"ttl_s": 10})
        assert too_short.status_code == 422
        assert too_short.json()["code"] == "BAD_REQUEST"

        # 下载脚本只嵌入一次性令牌（0.3.1 起不再接受共享注册码）
        cmd = client.get("/api/v1/public/install.cmd", params={"token": token})
        assert cmd.status_code == 200, cmd.text
        assert token in cmd.text, "install.cmd 必须携带一次性令牌"
        assert "@@ENROLL_CODE@@" not in cmd.text
        code_query = client.get("/api/v1/public/install.cmd", params={"code": "whatever"})
        assert code_query.status_code == 401, "注册码路径必须被拒绝"

        # 一次性：首次 enroll 消耗成功，复用失败（即使换设备）
        payload = {
            "uid": "OBS-A18-DEVICE",
            "hostname": "a18",
            "install_token": token,
        }
        first = client.post("/api/v1/agent/enroll", json=payload)
        assert first.status_code == 200, first.text

        replayed = dict(payload)
        replayed["uid"] = "OBS-REPLAY-OTHER"
        second = client.post("/api/v1/agent/enroll", json=replayed)
        assert second.status_code == 401, second.text
        assert "令牌" in second.json()["message"]

        # 绑定首台设备：列表里 used=True 且 used_device_id 指向首次注册的设备
        listed = client.get("/api/v1/install-tokens").json()["data"]["tokens"]
        assert listed and listed[0]["used"] is True
        assert listed[0]["used_device_id"] is not None

        # 同设备重消费放行（A17 自愈路径：401 后带同一令牌重新入网）
        again = dict(payload)
        again["install_token"] = token
        third = client.post("/api/v1/agent/enroll", json=again)
        assert third.status_code == 200, third.text


def test_a19_docs_and_version_consistency() -> None:
    """A19：README/docs 的版本、端口、项数、保留天数与代码一致；--check 覆盖文档。"""

    import subprocess

    from observatory.core.settings_spec import SPECS

    root = REPO_ROOT
    docs = {path.name: path.read_text(encoding="utf-8") for path in sorted((root / "docs").glob("*.md"))}
    readme = (root / "README.md").read_text(encoding="utf-8")

    # 保留天数：SPECS 默认值是唯一事实来源，三份用户文档必须写同样的数字
    retention_defaults = {
        "frame_retention_days": 14,
        "audit_retention_days": 180,
        "command_retention_days": 30,
    }
    for key, expected in retention_defaults.items():
        assert int(SPECS[key].default) == expected, f"{key} 默认值漂移"
    for name in ("部署.md", "使用.md", "合规与授权.md"):
        text = docs[name]
        assert "14 天" in text and "180 天" in text and "30 天" in text, f"{name} 保留天数与代码不一致"

    # 端口：容器/nginx/文档统一 8443
    assert "8443" in (root / "server/Dockerfile").read_text(encoding="utf-8")
    assert "8443" in (root / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    assert "8443" in readme
    for name in ("部署.md", "接口.md"):
        assert "8443" in docs[name], f"{name} 未写 8443"

    # 接口文档必须描述 v3 主链路且不再出现 0.2.0
    interface = docs["接口.md"]
    assert "/api/v1/agent/link" in interface
    assert "0.2.0" not in interface

    # 打包文档必须列出实际存在的 CI job（A20 的文档侧）
    packaging = docs["受控端打包.md"]
    workflow = (root / ".github/workflows/build.yml").read_text(encoding="utf-8")
    for job in ("deploy-static", "nginx-config-test"):
        assert f"  {job}:" in workflow
        assert job in packaging, f"受控端打包.md 未列出 CI job {job}"

    # 安装令牌 TTL 的旧 env 名必须从文档/样例彻底消失（A18/A19）
    assert "OBS_INSTALL_TOKEN_TTL_MINUTES" not in (root / "server/.env.example").read_text(encoding="utf-8")
    assert "OBS_INSTALL_TOKEN_TTL_MINUTES" not in docs["部署.md"]

    # --check 必须覆盖 README/docs/package-lock，且当前仓库是同步的
    completed = subprocess.run(
        [sys.executable, str(root / "tools/set_version.py"), "--check"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    for marker in ("README.md", "docs/*.md", "package-lock.json"):
        assert marker in completed.stdout, f"set_version --check 未覆盖 {marker}"


def test_a20_ci_gate_runs_required_jobs() -> None:
    """A20：CI 必跑 服务端自检 / 受控端逻辑 / 协议兼容 / WebUI 类型检查与构建。"""

    workflow = (REPO_ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8")
    assert "requirements-dev.txt" in workflow, "CI 必须用 requirements-dev.txt 安装测试依赖"
    for job in ("version:", "server:", "contract:", "acceptance:", "webui:", "agent-windows:"):
        assert re.search(rf"^  {re.escape(job)}", workflow, re.MULTILINE), f"缺少 job: {job}"
    # 服务端自检 / 受控端逻辑 / 协议兼容
    assert "tests/unit" in workflow and "tests/contract" in workflow and "tests/acceptance" in workflow
    assert "pytest" in workflow
    # WebUI 类型检查 + 构建
    assert "npm run build" in workflow
    # 版本一致性
    assert "set_version.py --check" in workflow
    # Windows runner 上构建/测试受控端
    assert "windows-latest" in workflow
    assert "build.ps1" in workflow
    # 兼容旧对外命令名
    assert "tools/test_server.py" in workflow


def test_a21_dead_code_is_gone_or_justified() -> None:
    """A21：审计列出的死代码全部消失，或有明确保留理由（旧模块删除前只允许留在白名单）。"""

    # 0.2.10 审计报告列出的死代码符号 / 旧 API 定义
    must_be_gone = (
        "AgentHello",
        "FileReadOut",
        "FrameUploadMeta",
        "EventOut",
        "acknowledged_uninstall",
        "def dumps(",
        "def runtime_or_none(",
        "def clockOffset(",
        "def read_exact(",
        "def get_state(",
    )
    #: 明确保留理由：这些是 v3 仍在使用的名字/兼容字段，不是死代码
    justified = {
        "EVENT_CONTROL_FRAME": "0.3.0 console/events.py 的实时事件名（非旧 schemas 常量）",
        "clock_offset_ms": "legacy 0.2.x 心跳字段的兼容读取（N2）",
        "last_server_time_ms": "legacy 0.2.x 心跳字段的兼容读取（N2）",
    }

    # 0.2.10 旧模块已全部删除：白名单必须为空，任何命中都是新架构里的死代码
    stale_allowlist: set[str] = set()
    roots = ["server", "agent", "tools", "usb-installer", "webui/src"]
    offenders: dict[str, list[str]] = {token: [] for token in must_be_gone}
    for root_name in roots:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".ts", ".tsx", ".js", ".ps1", ".cmd"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            relative = path.relative_to(REPO_ROOT).as_posix()
            for token in must_be_gone:
                if token in text:
                    offenders[token].append(relative)

    unexpected = {
        token: [path for path in paths if path not in stale_allowlist] for token, paths in offenders.items()
    }
    unexpected = {token: paths for token, paths in unexpected.items() if paths}
    assert not unexpected, f"死代码出现在新架构/非白名单文件: {unexpected}"
    assert justified, "保留理由必须显式登记（A21 的“或明确保留理由”）"

    # 旧模块不得被新装配 import（删除前的隔离证明）
    main = (REPO_ROOT / "server/observatory/main.py").read_text(encoding="utf-8")
    for stale in (".schemas import", ".services.commands", ".services.control"):
        assert stale not in main, f"main.py 仍引用旧模块: {stale}"


def test_a22_no_silent_except_without_log_or_suppress() -> None:
    """A22：静默 except 全部改为 contextlib.suppress 或带日志；ruff 禁止裸静默。"""

    import re

    server = REPO_ROOT / "server/observatory"
    new_dirs = ("core", "link", "media", "console", "adapters")
    files: list[Path] = []
    for directory in new_dirs:
        files += list((server / directory).rglob("*.py"))
    files += [server / "presence.py", server / "main.py", server / "cli.py"]

    silent = re.compile(r"except[^\n:]*:\s*(?:#.*)?\n\s*(?:pass|\.\.\.)\s*(?:#.*)?$", re.MULTILINE)
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{text[:m.start()].count(chr(10)) + 1}"
        for path in files
        if (text := path.read_text(encoding="utf-8")) and (m := silent.search(text))
    ]
    assert offenders == [], f"仍有静默 except（应改 contextlib.suppress 或记日志）: {offenders}"

    # ruff 必须禁止裸静默（BLE001：blind except；S110：try-except-pass）
    config = ""
    for name in ("pyproject.toml", "ruff.toml", ".ruff.toml"):
        candidate = REPO_ROOT / name
        if candidate.exists():
            config += candidate.read_text(encoding="utf-8")
    assert config, "缺少 ruff 配置（A22 要求静态规则禁止裸静默）"
    assert "BLE001" in config or "S110" in config, "ruff 配置未启用 BLE001/S110"


def test_a23_default_domains_only_in_one_config_module() -> None:
    """A23：默认域名只出现在 1 个配置模块 + 构建期注入，其余从配置读。"""

    domains = ("tianhw.top", "123423.xyz")
    server = REPO_ROOT / "server/observatory"

    server_new = []
    for directory in ("core", "link", "media", "console", "adapters"):
        server_new += list((server / directory).rglob("*.py"))
    server_new += [server / "presence.py", server / "main.py", server / "cli.py"]
    allowed_server = server / "core/config.py"
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in server_new
        if path.exists() and path != allowed_server and any(d in path.read_text(encoding="utf-8") for d in domains)
    ]
    assert offenders == [], f"新服务端层出现第二处默认域名: {offenders}"
    allowed_text = allowed_server.read_text(encoding="utf-8")
    assert "DEFAULT_CONSOLE_HOST" in allowed_text and "DEFAULT_AGENT_HOSTS" in allowed_text

    agent = REPO_ROOT / "agent/observatory_agent"
    agent_new = []
    for directory in ("core", "win", "tasks"):
        agent_new += list((agent / directory).rglob("*.py"))
    agent_new += [agent / "main.py", agent / "version.py"]
    allowed_agent = agent / "core/config.py"
    agent_offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in agent_new
        if path.exists() and path != allowed_agent and any(d in path.read_text(encoding="utf-8") for d in domains)
    ]
    assert agent_offenders == [], f"新受控端层出现第二处默认域名: {agent_offenders}"

    # 0.2.10 顶层残留不得被新装配 import（随后由 Lead 删除）
    main = (server / "main.py").read_text(encoding="utf-8")
    assert "from .core.config import" in main and "from .adapters.hostsplit import" in main
    assert "from .config import" not in main and "from .hostsplit import" not in main

    # 前端与 nginx 模板只能用配置/占位符
    for path in (REPO_ROOT / "webui/src").rglob("*"):
        if path.suffix in {".ts", ".tsx"}:
            text = path.read_text(encoding="utf-8")
            assert not any(d in text for d in domains), f"{path.relative_to(REPO_ROOT)} 写死生产域名"
    for template in (REPO_ROOT / "deploy/nginx").glob("*.template"):
        text = template.read_text(encoding="utf-8")
        assert not any(d in text for d in domains), f"{template.name} 写死生产域名（应用占位符）"


def test_a24_repo_hygiene() -> None:
    """A24：根 .dockerignore 生效；容器非 root + 只读根；.gitignore 无重复；.env.dev 不进生产。"""

    root = REPO_ROOT

    # 1) .dockerignore 必须在仓库根（compose 的 context 是 ..），旧 server/.dockerignore 已废
    dockerignore = root / ".dockerignore"
    assert dockerignore.is_file(), "缺少根级 .dockerignore（构建上下文是仓库根）"
    ignored = dockerignore.read_text(encoding="utf-8")
    for marker in (".localdata", "server/.env", "deploy", "docs", "webui/node_modules"):
        assert marker in ignored, f".dockerignore 未忽略 {marker}"
    stale_dockerignore = root / "server/.dockerignore"
    if stale_dockerignore.exists():
        stale_text = stale_dockerignore.read_text(encoding="utf-8")
        assert "此位置不生效" in stale_text and "根" in stale_text, (
            "server/.dockerignore 若保留，必须明确标注已废弃并指向根 .dockerignore（不能写规则）"
        )
        assert not any(line.strip() and not line.strip().startswith("#") for line in stale_text.splitlines()), (
            "server/.dockerignore 不能含实际规则（Docker 不会读取它）"
        )

    # 2) 容器非 root + 只读根 + 权限收敛 + 资源限制
    dockerfile = (root / "server/Dockerfile").read_text(encoding="utf-8")
    compose = (root / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    assert "USER observatory" in dockerfile
    assert "useradd" in dockerfile and "--uid" in dockerfile
    for marker in ("read_only: true", "user:", "cap_drop:", "no-new-privileges:true", "mem_limit:", "pids_limit:"):
        assert marker in compose, f"compose 缺少 {marker}"
    assert "127.0.0.1" in compose, "8443 默认必须只绑回环（A11/A24）"

    # 3) Ubuntu/Debian Docker 源分流（不再写死 Debian 源）
    setup = (root / "deploy/setup.sh").read_text(encoding="utf-8")
    assert "linux/ubuntu" in setup and "linux/debian" in setup
    assert "os-release" in setup and "ID_LIKE" in setup

    # 4) .gitignore 无重复条目
    lines = [
        line.strip()
        for line in (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    duplicates = {line for line in lines if lines.count(line) > 1}
    assert not duplicates, f".gitignore 重复条目: {duplicates}"

    # 5) .env.dev 不进生产路径：compose/Dockerfile/部署脚本只读 server/.env
    assert "../server/.env" in compose
    production_files = [root / "server/Dockerfile", root / "deploy/docker-compose.yml", *sorted((root / "deploy").glob("*.sh"))]
    for path in production_files:
        code_lines = "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
        )
        assert ".env.dev" not in code_lines, f"{path.name} 的生效配置引用 .env.dev"
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "cp server/.env.dev" not in readme, "README 不得教用户拷贝弱口令 .env.dev"


# ---------------------------------------------------------------------------
# 元测试：保证 24 条不缺号、所有 skip 都带 reason
# ---------------------------------------------------------------------------


def _numbered_acceptance_tests() -> dict[int, object]:
    module = sys.modules[__name__]
    found: dict[int, object] = {}
    for name, obj in inspect.getmembers(module, inspect.isfunction):
        match = re.match(r"test_a(\d{2})_", name)
        if match:
            found[int(match.group(1))] = obj
    return found


def test_meta_all_24_acceptance_ids_exist() -> None:
    found = _numbered_acceptance_tests()
    assert set(found) == set(range(1, 25)), f"缺失: {sorted(set(range(1, 25)) - set(found))}"


#: 这些编号已按规格实现并真实运行；其余编号必须先 skip(reason="W…")，不许静默通过。
ACTIVE_NOW = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24})


def test_meta_every_skipped_acceptance_test_has_reason() -> None:
    for number, func in _numbered_acceptance_tests().items():
        marks = getattr(func, "pytestmark", [])
        skip_marks = [mark for mark in marks if mark.name == "skip"]
        if number in ACTIVE_NOW:
            assert not skip_marks, f"A{number} 已实现，不能再 skip"
            continue
        assert skip_marks, f"A{number} 尚未实现时必须 skip（不要静默 xfail/占位通过）"
        assert str(skip_marks[0].kwargs.get("reason", "")).strip(), f"A{number} 的 skip 缺少 reason"