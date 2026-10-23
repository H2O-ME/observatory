"""A1–A24 Definition of Done（spec §3.1）。

W0 状态：断言已按规格写死，除 A20（CI 门禁，本波交付）外全部 `skip(reason="W…")`，
等对应波次接口就位后只需去掉装饰器，测试体本身不需要重写。

约定：
- 每个函数名以 `test_aNN_` 开头，编号与规格一一对应；`test_a20_` 现在必须通过。
- 所有 skip 必须带 reason（`test_meta_every_skip_has_reason` 会强制检查）。
- 断言一律针对**行为**（HTTP 状态、总线计数、报文形状），不依赖实现细节命名。
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.acceptance

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# A1–A24
# ---------------------------------------------------------------------------


@pytest.mark.windows_only
@pytest.mark.skip(reason="W2: 需要 agent/observatory_agent/win/ + tasks/uninstall.py 的 onedir helper")
def test_a01_uninstall_helper_onedir_ack_after_alive() -> None:
    """A1：真实 onedir 产物启动 helper 成功；ack 只在 helper 确认存活后发出。"""

    # given: 一个真实 onedir 目录（_internal/ 存在），helper 以 --purge 启动
    # when: helper 存活 2s 后被确认
    # then: POST /api/v1/agent/uninstall/ack 被调用一次
    # and: helper 启动失败时，设备保持 online 且上报 event=uninstall_failed
    raise AssertionError("W2: 需实现 win/tasks/uninstall 的 verify_helper_alive，再启用本测试")


@pytest.mark.windows_only
@pytest.mark.skip(reason="W2: 需要 win/service.py 的 restart 路径与 devices.boot_id（spec §6）")
def test_a02_restart_agent_actually_restarts() -> None:
    """A2：下发 restart_agent 后 ≤60s 重新上线，且 boot_id 变化。"""

    raise AssertionError("W2: 需要 boot_id 列 + service 重启，再启用")


@pytest.mark.skip(reason="W1: 需要 core/commands 校验 + agent tasks/update 校验链")
def test_a03_update_requires_sha256_and_aborts_on_mismatch() -> None:
    """A3：无摘要的 update 命令被拒（422）；摘要不符时中止且不替换。"""

    raise AssertionError("W1: 服务端 422 + 受控端摘要不符不换目录，再启用")


@pytest.mark.skip(reason="W1: 需要 core/settings 下发与 console DTO 的 SECRET_KEYS 过滤")
def test_a04_secrets_never_reach_agent_or_devices_api() -> None:
    """A4：下发给受控端的任何 config JSON 与 /devices 响应都不含 SECRET_KEYS。"""

    raise AssertionError("W1: 断言 config 与 /devices 响应不包含 archive_token 等键，再启用")


@pytest.mark.skip(reason="W1: 需要 link/session 的控制撤销与 TTL 关闭语义")
def test_a05_revoke_stops_ctrl_in_and_ttl_closes() -> None:
    """A5：revoke 后 1s 内 ctrl_in 不再转发；TTL 到期自动 ctrl_stop 并关 WS。"""

    raise AssertionError("W1: 撤销 + TTL 自动结束，再启用")


@pytest.mark.skip(reason="W2: 部署/证书属 release-ops（spec §8 W2）")
def test_a06_certificates_are_real_and_renewal_fails_loudly() -> None:
    """A6：部署产出实体证书（非悬空符号链接）；续期失败退出非 0。"""

    raise AssertionError("W2: deploy 证书链，再用容器/脚本断言")


@pytest.mark.skip(reason="W2: update.sh 备份与回滚属 release-ops")
def test_a07_upgrade_backs_up_and_rolls_back() -> None:
    """A7：升级前备份 DB + 记录 commit/镜像；健康失败自动回退且退出非 0。"""

    raise AssertionError("W2: update.sh 备份/回滚，再启用")


@pytest.mark.skip(reason="W1: 需要 core/clock 预算表与前端 deadline 消费（lib/api.ts）")
def test_a08_deadline_never_inverts_and_slow_device_succeeds() -> None:
    """A8：所有接口服务端 deadline < 客户端超时；注入 25s 延迟的文件操作成功。"""

    raise AssertionError("W1: 预算表 + 慢设备注入，再启用")


@pytest.mark.skip(reason="W1: 需要 link/session 的 async with 等待作用域（C3）")
def test_a09_no_waiter_leak_after_1000_commands() -> None:
    """A9：连续创建 1000 条不等待的命令后，等待者条目数回到 0。"""

    raise AssertionError("W1: 无全局 waiter 字典，断言计数归零，再启用")


@pytest.mark.skip(reason="W1: 需要 link/journal 的积压重投与幂等（spec §5.6）")
def test_a10_command_delivery_closure_after_reconnect() -> None:
    """A10：WS 断开期积压命令重连后 ≤1 心跳送达；回执丢失重投不重复执行。

    附带 §5.6 的 ack 语义（方向相反，容易写反）：
    - 发 3 条上行 a,b,c → 响应 `ack_id == c`（服务端已处理的上行 id）；
    - 再发 `last_ack_id=<下行 id>` → 服务端不再重发该下行。
    """

    from fastapi.testclient import TestClient

    from observatory.main import create_app
    from tests.contract.fixtures import agent_0_2_10 as legacy

    with TestClient(create_app()) as client:
        token = client.post(
            "/api/v1/agent/enroll", json=legacy.enroll_request(code="test-enroll-code")
        ).json()["token"]
        headers = {"X-Device-Token": token}
        items = [
            {
                "v": 3,
                "id": uplink_id,
                "t": "event",
                "ts_ms": 1_730_000_000_000,
                "body": {"event": "noop", "data": {"seq": index}},
            }
            for index, uplink_id in enumerate(("a", "b", "c"))
        ]
        response = client.post(
            "/api/v1/agent/link", headers=headers, json={"v": 3, "last_ack_id": "", "items": items}
        )
        assert response.status_code == 200
        assert response.json()["ack_id"] == "c"

        first = client.post("/api/v1/agent/link", headers=headers, json={"v": 3, "last_ack_id": "", "items": []})
        downlink_ids = [item["id"] for item in first.json()["items"]]
        if downlink_ids:
            confirmed = downlink_ids[0]
            second = client.post(
                "/api/v1/agent/link",
                headers=headers,
                json={"v": 3, "last_ack_id": confirmed, "items": []},
            )
            assert confirmed not in [item["id"] for item in second.json()["items"]]


@pytest.mark.skip(reason="W1: 需要 adapters/hostsplit fail-closed 与 Settings 默认值")
def test_a11_default_bind_and_forwarded_headers_and_hostsplit() -> None:
    """A11：默认 127.0.0.1、trust_forwarded_for=false、HostSplit 配置不全 fail-closed。"""

    raise AssertionError("W1: 三个安全默认值，再启用")


@pytest.mark.skip(reason="W1: 需要 console DTO 的 server_time_ms 与 webui/lib/clock.ts")
def test_a12_single_clock_source() -> None:
    """A12：FileListOut.server_time_ms 来自服务端；前端无 Date.now 参与新鲜度。"""

    raise AssertionError("W1: 服务端注入 + 前端只读服务端时钟，再启用")


@pytest.mark.skip(reason="W1: 需要 presence.capabilities 联合推导（spec §4.1）")
def test_a13_capabilities_are_exact() -> None:
    """A13：control_enabled=false → control=false；无包 → update=false；无通道 → files=false。"""

    raise AssertionError("W1: capabilities 矩阵，再启用")


@pytest.mark.skip(reason="W2: 归档/保留属 server-media（spec §8 W2）")
def test_a14_archive_closure() -> None:
    """A14：上传成功才删本地；删除设备递归删远端；失败进重试队列且可观测。"""

    raise AssertionError("W2: 归档状态表 + 重试，再启用")


@pytest.mark.skip(reason="W1: 需要 Settings/SPECS 的消费者测试基础设施")
def test_a15_every_setting_key_has_a_consumer() -> None:
    """A15：SPECS 每键都有消费者测试；agent_offline_after_s 真的改变 offline 阈值。"""

    raise AssertionError("W1: 遍历 SPECS 断言有消费者，再启用")


@pytest.mark.skip(reason="W2: 坐标/DPI 属 agent tasks/input.py")
def test_a16_multimonitor_coordinates_within_2px() -> None:
    """A16：双屏负坐标 / DPI150% 点击误差 ≤2px（纯函数）。"""

    raise AssertionError("W2: 见 tests/unit/test_coords.py，再启用")


@pytest.mark.windows_only
@pytest.mark.skip(reason="W2: 管道 ACL 与 401 重注册属 agent win/ 层")
def test_a17_pipe_acl_and_401_reregistration() -> None:
    """A17：管道 ACL 仅 SYSTEM/Administrators/当前用户；401 后自动重注册回主链路。"""

    raise AssertionError("W2: 管道 DACL + 401 链路，再启用")


@pytest.mark.skip(reason="W2: 安装令牌属 release-ops（install_tokens 表，spec §6）")
def test_a18_install_token_single_use_ttl_bound() -> None:
    """A18：安装令牌一次性 + TTL + 绑定首台设备；.cmd 不含长期注册码。"""

    raise AssertionError("W2: install_tokens + 模板渲染，再启用")


@pytest.mark.skip(reason="W3: 文档与版本一致性属最后收口")
def test_a19_docs_and_version_consistency() -> None:
    """A19：README/docs 的版本、端口、项数、保留天数与代码一致；--check 覆盖文档。"""

    raise AssertionError("W3: 文档校验脚本，再启用")


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


@pytest.mark.skip(reason="W3: 死代码清理在最后收口（A21 清单见 tmp_w0/acceptance.md）")
def test_a21_dead_code_is_gone_or_justified() -> None:
    """A21：审计列出的 20 处死代码全部消失，或有明确保留理由。"""

    banned = (
        "AgentHello",
        "FileReadOut",
        "FrameUploadMeta",
        "EventOut",
        "acknowledged_uninstall",
        "EVENT_CONTROL_FRAME",
        "clock_offset_ms",
        "last_server_time_ms",
        "def dumps(",
        "def runtime_or_none(",
        "def clockOffset(",
        "def read_exact(",
        "def get_state(",
    )
    raise AssertionError(f"W3: 静态扫描 {banned} 全部消失或登记保留理由，再启用")


@pytest.mark.skip(reason="W3: 异常可观测收口在 W3（ruff 规则 + suppress 改造）")
def test_a22_no_silent_except_without_log_or_suppress() -> None:
    """A22：静默 except 全部改为 contextlib.suppress 或带日志；ruff 禁止裸静默。"""

    raise AssertionError("W3: 扫描 146 处静默分支，再启用")


@pytest.mark.skip(reason="W3: 域名集中属最后收口（A23）")
def test_a23_default_domains_only_in_one_config_module() -> None:
    """A23：默认域名只出现在 1 个配置模块 + 构建期注入，其余从配置读。"""

    raise AssertionError("W3: 全仓扫描 tianhw.top / 123423.xyz，再启用")


@pytest.mark.skip(reason="W3: 仓库卫生属最后收口（A24）")
def test_a24_repo_hygiene() -> None:
    """A24：根 .dockerignore 生效；容器非 root + 只读根；.gitignore 无重复；.env.dev 不进生产。"""

    raise AssertionError("W3: 仓库卫生检查，再启用")


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


def test_meta_every_skipped_acceptance_test_has_reason() -> None:
    for number, func in _numbered_acceptance_tests().items():
        marks = getattr(func, "pytestmark", [])
        skip_marks = [mark for mark in marks if mark.name == "skip"]
        if number == 20:
            assert not skip_marks, "A20 在 W0 必须真实运行"
            continue
        assert skip_marks, f"A{number} 尚未实现时必须 skip（不要静默 xfail/占位通过）"
        assert str(skip_marks[0].kwargs.get("reason", "")).strip(), f"A{number} 的 skip 缺少 reason"