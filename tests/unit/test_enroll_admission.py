"""A4：启用服务端 OBS_ENROLL_KEY 后受控端仍能注册（三条链路：安装参数→配置→请求体）。

服务端契约（``server/observatory/console/enroll.py`` 的 ``_require_admission_key``）：

* 只在"创建新设备行 / 重新准入已卸载设备"时校验；已有活跃设备的令牌轮换不校验
  （换 key 不会把在跑的机器踢下线）；
* 请求体字段名 ``code`` 或 ``enroll_code`` 均可，值为密钥原文，常量时间比较；
* 密钥来自服务端 ``OBS_ENROLL_KEY``；默认空 = 完全开放。

受控端因此必须：没配置密钥时**绝不**发送该字段（零凭据注册），配置了才发。
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


# ----------------------------------------------------------------- 配置层 ---
def test_enroll_key_defaults_to_open_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    from observatory_agent.core import config  # noqa: PLC0415

    monkeypatch.delenv("OBS_ENROLL_KEY", raising=False)
    assert config.resolve_enroll_key(None) == ""
    assert config.AgentConfig().enroll_key == ""


def test_enroll_key_prefers_config_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from observatory_agent.core import config  # noqa: PLC0415

    monkeypatch.setenv("OBS_ENROLL_KEY", "from-env")
    assert config.resolve_enroll_key("") == "from-env"
    # 配置文件优先：安装器写进 config.json 的值比服务的环境块更可靠。
    assert config.resolve_enroll_key("from-file") == "from-file"


def test_enroll_key_roundtrips_through_config_file(tmp_dir) -> None:
    from observatory_agent.core import config  # noqa: PLC0415

    path = tmp_dir / "config.json"
    saved = config.AgentConfig(device_uid="OBS-A4", enroll_key="secret-key")
    saved.save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["enroll_key"] == "secret-key"

    loaded = config.AgentConfig.load(path)
    assert loaded.enroll_key == "secret-key"


def test_empty_enroll_key_is_not_written_to_disk(tmp_dir) -> None:
    """未配置密钥时配置文件里不能出现该键（保持零凭据语义）。"""

    from observatory_agent.core import config  # noqa: PLC0415

    path = tmp_dir / "config.json"
    config.AgentConfig(device_uid="OBS-A4-EMPTY").save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "enroll_key" not in raw


# ------------------------------------------------------------- 安装参数链路 ---
def test_install_arg_persists_enroll_key(tmp_dir, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--install --enroll-code X`` 必须把 X 写进配置（旧实现是空操作）。"""

    from observatory_agent import main  # noqa: PLC0415
    from observatory_agent.core import config  # noqa: PLC0415

    monkeypatch.delenv("OBS_ENROLL_KEY", raising=False)
    args = main.build_parser().parse_args(["--install", "--enroll-code", "deploy-key-123"])
    changes = main._cli_config_changes(args)  # noqa: SLF001 - 纯函数，直接断言
    assert changes["enroll_key"] == "deploy-key-123"

    path = tmp_dir / "config.json"
    config.AgentConfig(device_uid="OBS-A4-ARG").save(path)
    stored = config.AgentConfig.load(path)
    stored.enroll_key = changes["enroll_key"]
    stored.save(path)
    assert config.AgentConfig.load(path).enroll_key == "deploy-key-123"


def test_install_token_argument_is_accepted_as_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧脚本传 ``--install-token`` 时仍能把值带进配置（否则老脚本装完注册不上）。"""

    from observatory_agent import main  # noqa: PLC0415

    monkeypatch.delenv("OBS_ENROLL_KEY", raising=False)
    args = main.build_parser().parse_args(["--install", "--install-token", "legacy-value"])
    assert main._cli_config_changes(args)["enroll_key"] == "legacy-value"


def test_install_without_key_writes_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    from observatory_agent import main  # noqa: PLC0415

    monkeypatch.delenv("OBS_ENROLL_KEY", raising=False)
    args = main.build_parser().parse_args(["--install"])
    assert "enroll_key" not in main._cli_config_changes(args)


# --------------------------------------------------------------- 请求体链路 ---
class _Recorder:
    """最小 httpx.Response 替身：记录请求体并返回固定 JSON。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.requests: list[dict] = []

    def response(self) -> "_Recorder":
        return self

    async def _request(self, method, path, *, json=None, timeout=None, headers=None):  # noqa: A002
        self.requests.append({"method": method, "path": path, "json": json})
        return _FakeResponse(self._payload)


class _FakeResponse:
    status_code = 200
    headers: dict = {}

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload)


def _link(recorder: _Recorder):
    from observatory_agent.core import config as config_module  # noqa: PLC0415
    from observatory_agent.core.link import Link  # noqa: PLC0415
    from observatory_agent.core.outbox import Outbox, OutboxLimits  # noqa: PLC0415

    link = Link(
        token="",
        base_urls=["https://example.invalid"],
        device_uid="OBS-A4",
        agent_version="0.0.0",
        boot_id="boot",
        clock=config_module.Clock(),
        outbox=Outbox(OutboxLimits(max_items=8, max_bytes=1024 * 1024, overflow="drop_oldest")),
    )
    # 只替换 HTTP 往返，保留 enroll() 的真实请求体构造逻辑。
    link._request = recorder._request  # type: ignore[method-assign] # noqa: SLF001
    link._raise_for_status = lambda *a, **k: None  # noqa: SLF001
    return link


@pytest.fixture(scope="module")
def enrollment():
    """复用一个 Link：构造会加载 SSL 默认证书链（约 2s），每个用例重建太慢。

    每个用例自己清空 ``requests``，因此断言仍然相互独立。
    """

    recorder = _Recorder({"token": "t", "base_urls": ["https://example.invalid"]})
    yield _link(recorder), recorder
    recorder.requests.clear()


async def _enroll(link, **kwargs):
    return await link.enroll(
        device_uid="OBS-A4",
        hostname="host",
        os_name="Windows",
        os_version="11",
        arch="x86_64",
        agent_version="0.0.0",
        sysinfo={},
        sessions=[],
        **kwargs,
    )


def test_enroll_sends_code_when_configured(enrollment) -> None:
    import asyncio  # noqa: PLC0415

    link, recorder = enrollment
    recorder.requests.clear()
    asyncio.run(_enroll(link, code="deploy-key-123"))
    body = recorder.requests[0]["json"]
    assert body["code"] == "deploy-key-123"
    # 服务端接受的另一个别名不重复发送（只发一种，避免两边不一致）。
    assert "enroll_code" not in body


def test_enroll_omits_code_when_unset(enrollment) -> None:
    """零凭据注册：没有密钥时请求体里**不能**出现 code 字段。"""

    import asyncio  # noqa: PLC0415

    link, recorder = enrollment
    recorder.requests.clear()
    asyncio.run(_enroll(link))
    body = recorder.requests[0]["json"]
    assert "code" not in body
    assert "enroll_code" not in body
    assert "install_token" not in body, "旧票据语义必须彻底消失"
    # 服务端契约要求的身份字段仍要齐全。
    for key in ("uid", "hostname", "os_name", "os_version", "arch", "agent_version", "sysinfo", "sessions"):
        assert key in body, key


def test_enroll_rejects_blank_code(enrollment) -> None:
    """空白字符串按"未配置"处理，不能变成"带着空密钥去撞 401"。"""

    import asyncio  # noqa: PLC0415

    link, recorder = enrollment
    recorder.requests.clear()
    asyncio.run(_enroll(link, code="   "))
    assert "code" not in recorder.requests[0]["json"]


def test_blank_local_token_does_not_send_device_header(enrollment) -> None:
    """注册发生在拿到令牌之前：请求头里不该有 X-Device-Token。

    服务端契约里 ``X-Device-Token`` 是"证明持有令牌"的凭据；首次注册带一个
    空值/旧值会让判定路径变得难以预测，所以 enroll 请求不携带设备令牌。
    """

    import asyncio  # noqa: PLC0415

    from observatory_agent.core.link import DEVICE_TOKEN_HEADER  # noqa: PLC0415

    link, recorder = enrollment
    recorder.requests.clear()

    seen: dict = {}

    async def _capture(method, path, *, json=None, timeout=None, headers=None):  # noqa: A002
        seen.update(headers or {})
        recorder.requests.append({"method": method, "path": path, "json": json})
        return _FakeResponse({"token": "t"})

    link._request = _capture  # type: ignore[method-assign] # noqa: SLF001
    asyncio.run(_enroll(link, code="k"))
    assert seen.get(DEVICE_TOKEN_HEADER, "") == ""
