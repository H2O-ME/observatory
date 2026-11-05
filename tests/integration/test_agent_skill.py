"""0.3.19：Agent 接入端点（status + skill 下载）与 exec 默认开启。

契约：
- 两个端点都要求 console 认证（api_key 或 JWT），未认证 401；
- status 只回布尔位（api_key 永不回显真值）；
- download 是合法 zip，含 SKILL.md / scripts/obs.py / README.txt，占位符全部
  渲染完成（不允许 @@ 残留），且不包含任何密钥材料；
- exec_enabled 服务端默认已改为 True（0.3.19 决策：所有设备默认开启）。
"""

from __future__ import annotations

import io
import zipfile

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from tests.helpers import running_app  # noqa: PLC0415

    with running_app(tmp_path_factory.mktemp("agent-skill")) as running:
        yield running


def _admin(client) -> dict[str, str]:
    from tests.helpers import TEST_ADMIN_PASSWORD  # noqa: PLC0415

    response = client.post("/api/v1/auth/login", json={"password": TEST_ADMIN_PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['token']}"}


def test_skill_status_requires_auth(client) -> None:
    response = client.get("/api/v1/console/agent-skill/status")
    assert response.status_code == 401


def test_skill_status_reflects_api_key_without_leaking(client) -> None:
    headers = _admin(client)
    fresh = client.get("/api/v1/console/agent-skill/status", headers=headers).json()["data"]
    assert fresh["api_key_set"] is False
    assert fresh["version"].count(".") == 2
    assert isinstance(fresh["console_host"], str)

    import secrets

    key = f"obs-{secrets.token_hex(16)}"
    patched = client.patch("/api/v1/settings", json={"values": {"api_key": key}}, headers=headers)
    assert patched.status_code == 200, patched.text

    after = client.get("/api/v1/console/agent-skill/status", headers=headers).json()["data"]
    assert after["api_key_set"] is True
    assert key not in patched.text and key not in str(after), "密钥真值绝不能出现在任何响应里"


def test_skill_download_requires_auth(client) -> None:
    client.cookies.clear()  # 模块级 client：前面用例的登录 cookie 不能算认证
    assert client.get("/api/v1/console/agent-skill/download").status_code == 401


def test_skill_download_returns_rendered_zip(client) -> None:
    response = client.get("/api/v1/console/agent-skill/download", headers=_admin(client))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")
    assert "attachment" in response.headers.get("content-disposition", "")

    bundle = zipfile.ZipFile(io.BytesIO(response.content))
    names = set(bundle.namelist())
    assert "observatory-agent-skill/SKILL.md" in names
    assert "observatory-agent-skill/scripts/obs.py" in names
    assert "observatory-agent-skill/README.txt" in names

    skill = bundle.read("observatory-agent-skill/SKILL.md").decode("utf-8")
    cli = bundle.read("observatory-agent-skill/scripts/obs.py").decode("utf-8")
    readme = bundle.read("observatory-agent-skill/README.txt").decode("utf-8")
    for name, text in (("SKILL.md", skill), ("obs.py", cli), ("README.txt", readme)):
        assert "@@" not in text, f"{name} 仍有未渲染的占位符"
    assert skill.startswith("---\n"), "SKILL.md 必须带 frontmatter（skill 目录格式）"
    _assert_skill_contract(skill, cli)
    assert "OBS_ALLOW_PRIVATE" in readme, "README 必须写清 fake-ip 场景（F1）"
    assert ".agents/skills" in readme, "README 必须覆盖非 ZCode 的 agent 目录约定"


def _assert_skill_contract(skill: str, cli: str) -> None:
    """0.3.20 文档契约（实测报告 A1-A9 的防复发钉子）。"""

    # A1：读写走 /files/raw；/files/op 的 read/write 教法不得复活
    assert "/files/raw" in skill and "X-Eof" in skill and "truncate" in skill
    assert '"op":"read"' not in skill and '"op":"write"' not in skill
    assert "已废弃" in skill
    # A2：错误码含 AUTH/GONE；A5：信封双层 data；A6：capabilities 门控
    for needle in ("AUTH", "GONE", "data.data", "capabilities"):
        assert needle in skill, needle
    # A4/A8：WebP 与截图新鲜度
    assert "WebP" in skill and "ts_ms > 下发时刻" in skill
    # A9：frontmatter 带版本（渲染后应有 version: "0."）
    assert 'version: "0.' in skill
    # C 系：CLI 必备子命令与边界校验（分片响应头按 CLI 的小写取用形态断言）
    for needle in ("def cmd_get", "def cmd_put", "def cmd_settings", "def cmd_shot",
                   "x-file-size", "x-eof", "OBS_ALLOW_PRIVATE", "OBS_ALLOW_HTTP",
                   "sys.stdout.reconfigure", "_PinnedHTTPS", "def cmd_exec"):
        assert needle in cli, needle


def test_files_op_rejects_read_write_before_reachability(client) -> None:
    """A1 的服务端契约：op=read/write 一律 422 指向 /files/raw（无需活动链路）。"""

    import secrets

    headers = _admin(client)
    uid = f"OBS-SKILL-{secrets.token_hex(3).upper()}"
    from tests.helpers import enroll_device  # noqa: PLC0415

    enroll_device(client, uid=uid)
    for op in ("read", "write"):
        response = client.post(
            f"/api/v1/devices/{uid}/files/op",
            json={"op": op, "path": "C:\\x"},
            headers=headers,
        )
        assert response.status_code == 422, (op, response.text)
        assert "/files/raw" in response.text, op


def test_exec_enabled_defaults_to_true(client) -> None:
    headers = _admin(client)
    values = client.get("/api/v1/settings", headers=headers).json()["data"].get("values") or {}
    assert values.get("exec_enabled") is True, "0.3.19 起 exec 必须默认开启"
