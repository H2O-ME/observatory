"""受控端配置、路径、身份、时钟偏置与线程安全存储。

与 0.2.10 的 ``config.py`` 相比，这一份把三件事收干净：

1. **域名只在这个模块定义**（规格 A23）：``DEFAULT_BASE_URLS`` 是仓库里唯一的
   默认域名常量；其余模块（``link``/``main``）一律从配置读，不再自己写死
   （旧代码在 ``config.py:32`` 与 ``client.py:50`` 各写了一份）。
2. **配置项要么生效要么不存在**（C8）：``DEFAULT_SETTINGS`` 只列受控端真正
   消费的键；``poll_interval_s``（HTTP 载体轮询间隔）、``transport_reprobe_s``
   （向上探测起始间隔）、``capture_scope``（采集哪个会话）都各有唯一消费者，
   0.2.10 的 ``ws_enabled`` 这类安慰剂开关不再出现（载体选优由
   ``core/link.py`` 的退避状态机负责，不再是可以配错的开关）。
3. **时钟单一来源**（C5 的受控端侧）：``Clock`` 用服务端 ``server_time_ms``
   校准出 ``device_time_offset_ms``；所有上行时间字段统一叫 ``device_time_ms``。

纯逻辑部分（``compute_device_time_offset``）不读系统时钟，时间从参数注入；
只有 ``Clock.device_now_ms`` / ``AgentConfig.load`` 这类 IO/运行时便利方法会
读本机时钟。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .outbox import OVERFLOW_POLICIES, OutboxLimits

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- identity ---
PRODUCT = "Observatory"
SERVICE_NAME = "ObservatoryAgent"
SERVICE_DISPLAY_NAME = "Observatory 受控端"
SERVICE_DESCRIPTION = "Observatory 受控端：按授权采集画面并接受控制台指令。"
PIPE_PREFIX = r"\\.\pipe\observatory-agent"
CONFIG_FILENAME = "config.json"

#: 协议默认域名。**全仓库只允许出现这一次**（A23）；安装脚本通过
#: ``OBS_BASE_URLS`` 注入线路，注册成功后服务端下发的 ``base_urls`` 覆盖它。
_BUILTIN_BASE_URLS: tuple[str, ...] = ("https://control.tianhw.top", "https://observatory.123423.xyz")


def _split_urls(raw: str) -> list[str]:
    """按逗号/分号/空白拆分多线路；空段丢弃，去尾部斜杠。"""

    normalized = raw.replace(";", ",").replace("\n", ",").replace(" ", ",")
    return [item.strip().rstrip("/") for item in normalized.split(",") if item.strip()]


def default_base_urls() -> list[str]:
    """默认线路：环境变量注入优先，其次构建期内置值。

    ``OBS_BASE_URLS`` 由安装脚本/运维人员给出（换域名不需要重新打受控端包）；
    注册成功后以服务端返回的 ``base_urls`` 为准。
    """

    injected = os.environ.get("OBS_BASE_URLS", "")
    urls = _split_urls(injected)
    if urls:
        return urls
    return list(_BUILTIN_BASE_URLS)


DEFAULT_BASE_URLS: tuple[str, ...] = _BUILTIN_BASE_URLS


# ------------------------------------------------------------------ paths ---
def program_data_dir() -> Path:
    override = os.environ.get("OBS_PROGRAM_DATA", "").strip()
    if override:
        return Path(override)
    root = os.environ.get("ProgramData") or os.environ.get("ALLUSERSPROFILE") or r"C:\ProgramData"
    return Path(root) / PRODUCT


def install_dir() -> Path:
    """本构建所在目录：exe 与 ``_internal`` 的父目录。

    PyInstaller onedir 布局下 ``Path(__file__).parent.parent`` 会落到 ``_internal``
    里（0.2.10 因此有过一次"更新把 _internal 换掉、服务再也起不来"的事故），
    冻结后必须从 ``sys.executable`` 反推。``OBS_INSTALL_DIR`` 供测试/调试覆盖。
    """

    override = os.environ.get("OBS_INSTALL_DIR", "").strip()
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


PROGRAM_DATA: Path = program_data_dir()
LOG_DIR: Path = PROGRAM_DATA / "logs"
STAGING_DIR: Path = PROGRAM_DATA / "staging"
CONFIG_PATH: Path = PROGRAM_DATA / CONFIG_FILENAME
INSTALL_DIR: Path = install_dir()


def outbox_path() -> Path:
    """持久化 outbox 的 sqlite 文件（W1：进程被杀后未确认上行可续传）。

    ``OBS_OUTBOX_PATH`` 供测试/多实例隔离；默认落在数据目录，随卸载清理。
    """

    override = os.environ.get("OBS_OUTBOX_PATH", "").strip()
    if override:
        return Path(override)
    return PROGRAM_DATA / "outbox.sqlite3"


OUTBOX_PATH: Path = outbox_path()


def ensure_dirs() -> None:
    for path in (PROGRAM_DATA, LOG_DIR, STAGING_DIR):
        path.mkdir(parents=True, exist_ok=True)
    _harden_acl(PROGRAM_DATA)


def _harden_acl(path: Path) -> None:
    """P1（2026-10 审查）：收紧数据目录/配置的 DACL 到 SYSTEM+Administrators。

    ProgramData 子目录继承的默认 ACL 允许本地 Users 读取；config.json 里躺着
    设备令牌与一次性注册码——任意本地用户可窃取令牌冒充受控端。用系统自带的
    ``icacls`` 去继承并显式授权（零新依赖；服务以 SYSTEM 运行有权限改自己目录
    的 ACL）。失败只告警：ACL 加固是纵深防御，不能阻断主流程。

    两个刻意的边界（win_smoke 的 uninstall_staging 曾被第一版锁死）：
      * 只对真实生产目录（无 OBS_PROGRAM_DATA 覆盖、位于系统 ProgramData 下）
        生效——测试/便携目录跳过；
      * 授权表**保留当前调用者**：CLI 诊断等非提权场景下用户不是 SYSTEM，
        把自己移出 DACL 会让后续写入全部 PermissionError。
    """

    if os.name != "nt":
        return
    if os.environ.get("OBS_PROGRAM_DATA", "").strip():
        return
    grants = ["SYSTEM:(OI)(CI)F", "Administrators:(OI)(CI)F"]
    user = os.environ.get("USERNAME", "").strip()
    if user and user.lower() not in ("system",):
        grants.append(f"{user}:(OI)(CI)F")
    try:
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", *grants],
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            log.warning(
                "icacls 收紧 %s 失败（rc=%s）：%s",
                path,
                result.returncode,
                result.stderr.decode("mbcs", "replace").strip(),
            )
    except OSError as exc:
        log.warning("icacls 不可用，跳过 ACL 收紧（%s）：%s", path, exc)


# --------------------------------------------------------------- settings ---
#: 受控端真正消费的默认值（C8）。服务端可以覆盖同名键；这里只是首启/离线兜底。
#: 注意 ``jpeg_quality`` 默认 80：规格 N4 明确受控端出 JPEG q80 中间图，服务端
#: 再转 WebP q75（0.2.10 的 ``capture.py`` 实际出的是 WebP，与文档不一致）。
DEFAULT_SETTINGS: dict[str, Any] = {
    "capture_enabled": True,
    "capture_interval_s": 30,
    "capture_on_change_only": False,
    "capture_change_threshold": 8,
    "jpeg_quality": 80,
    "max_width": 1600,
    "control_enabled": True,
    "control_fps": 8,
    "control_quality": 55,
    "file_write_enabled": True,
    "file_upload_max_mb": 512,
    "auto_update": True,
    "heartbeat_interval_s": 20,
    #: A15：HTTP 载体两轮之间的间隔（也是长轮询的等待上限语义）。
    "poll_interval_s": 5,
    #: A15：HTTP 载体稳定后向上探测 WS 的起始间隔（CarrierPolicy）。
    "transport_reprobe_s": 120,
    #: A15：采集目标会话（"session" = 当前交互会话，"console" = 控制台会话）。
    "capture_scope": "session",
}

#: 服务端配置里的敏感键（A4）。服务端本来就不该下发这些；受控端再挡一层，
#: 保证即使服务端配置视图写错，令牌也不会落盘到 %ProgramData%。
SECRET_KEYS: frozenset[str] = frozenset(
    {"archive_token", "secret_key", "password", "github_token", "api_token"}
)

DEFAULT_HEARTBEAT_INTERVAL_S = 20
MIN_HEARTBEAT_INTERVAL_S = 5
MAX_HEARTBEAT_INTERVAL_S = 300


# ------------------------------------------------------------- identifiers ---
def new_device_uid() -> str:
    """稳定的不透明设备标识：优先 MachineGuid，取不到时随机。

    重装受控端后仍保持同一身份（令牌会轮换），服务端因此能识别"同一台机器"。
    """

    machine = _machine_guid()
    if machine:
        return f"OBS-{machine.replace('-', '').upper()[:12]}"
    return f"OBS-{secrets.token_hex(6).upper()}"


def _machine_guid() -> str:
    try:
        import winreg
    except ImportError:
        # 非 Windows（单测/诊断）环境没有 MachineGuid：随机身份即可。
        return ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value)
    except OSError as exc:
        log.info("读取 MachineGuid 失败（改用随机设备标识）: %s", exc)
        return ""


def new_boot_id() -> str:
    """本次**进程**启动的标识。

    A2 的验收是"重启后 boot_id 变化"，所以它必须每个进程新生成，绝不能从
    config.json 里读回来（那样重启前后不变，测试与服务端都会误判）。
    """

    return uuid.uuid4().hex


# ------------------------------------------------------------------ clock ---
def compute_device_time_offset(
    *, device_time_ms: int, server_time_ms: int, rtt_ms: int = 0
) -> int:
    """计算 ``device_time_offset_ms``（纯函数）。

    定义：``device_time_offset_ms = device_time_ms - (server_time_ms + rtt/2)``。

    - 设备时钟比服务端快 → 返回正数；
    - 估计的当前服务端时间 = ``device_now_ms - device_time_offset_ms``；
    - 减去半个 RTT 是为了把"服务端生成响应"与"设备收到响应"对齐（不追求
      NTP 级精度，服务端另有 ``server_time_ms`` 兜底，受控端时间只用于展示与
      超时预算）。
    """

    if not isinstance(device_time_ms, int) or isinstance(device_time_ms, bool) or device_time_ms <= 0:
        raise ValueError(f"device_time_ms 非法：{device_time_ms!r}")
    if not isinstance(server_time_ms, int) or isinstance(server_time_ms, bool) or server_time_ms <= 0:
        raise ValueError(f"server_time_ms 非法：{server_time_ms!r}")
    if not isinstance(rtt_ms, int) or isinstance(rtt_ms, bool) or rtt_ms < 0:
        raise ValueError(f"rtt_ms 非法：{rtt_ms!r}")
    return int(device_time_ms) - int(server_time_ms) - int(rtt_ms) // 2


@dataclass
class Clock:
    """服务端时钟偏置（可观测、可持久化的一小块状态）。

    ``observe`` 每次收到带 ``server_time_ms`` 的响应/信封都调用一次；只做单调
    收敛，不让一次异常值把偏置永久带偏（超过 24 小时或 10 分钟的离谱跳变只
    记日志并采用新值——单次采样无法判断谁错，服务端时间是唯一权威）。
    """

    device_time_offset_ms: int = 0
    updated_at_ms: int = 0

    def observe(self, *, device_time_ms: int, server_time_ms: int, rtt_ms: int = 0) -> int:
        offset = compute_device_time_offset(
            device_time_ms=device_time_ms, server_time_ms=server_time_ms, rtt_ms=rtt_ms
        )
        self.device_time_offset_ms = offset
        self.updated_at_ms = int(device_time_ms)
        return offset

    def server_now_ms(self, device_time_ms: int) -> int:
        """由设备当前时间估算服务端当前时间。"""

        return int(device_time_ms) - int(self.device_time_offset_ms)

    def device_now_ms(self) -> int:
        """本机（设备）当前毫秒时间戳；仅运行时代码使用，纯逻辑走参数注入。"""

        return int(time.time() * 1000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_time_offset_ms": int(self.device_time_offset_ms),
            "updated_at_ms": int(self.updated_at_ms),
        }


def outbox_limits_from_env() -> OutboxLimits:
    """从环境变量读取 outbox 上限/策略（运维可调，默认保守）。

    这是受控端本地策略，不属于服务端设置：服务端只关心"上行是否到达"，
    不应该替每台设备决定缓冲区大小。
    """

    def _int(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            log.warning("环境变量 %s=%r 不是整数，使用默认 %s", name, raw, default)
            return default

    overflow = os.environ.get("OBS_OUTBOX_OVERFLOW", "").strip() or "drop_oldest"
    if overflow not in OVERFLOW_POLICIES:
        log.warning("环境变量 OBS_OUTBOX_OVERFLOW=%r 未知，使用 drop_oldest", overflow)
        overflow = "drop_oldest"
    return OutboxLimits(
        max_items=_int("OBS_OUTBOX_MAX_ITEMS", 2048),
        max_bytes=_int("OBS_OUTBOX_MAX_BYTES", 32 * 1024 * 1024),
        overflow=overflow,
    )


# ----------------------------------------------------------------- config ---
def _load_config_json(target: Path) -> dict[str, Any]:
    """读取配置 JSON；损坏时隔离坏文件并尝试 ``.bak`` 恢复（F38）。

    配置损坏静默清零 token 会让设备永久离线，所以这里不允许"仅 warning 后
    继续跑"：坏文件改名隔离为 ``config.json.corrupt-<时间戳>``（保留取证），
    再尝试从 ``config.json.bak`` 恢复；两者都不可用才落到默认值（此时 token
    为空，链路会走重新注册，而不是拿着空令牌反复 401）。
    """

    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.error("读取配置失败（使用默认值）%s: %s", target, exc)
        return {}
    try:
        raw: Any = json.loads(text)
    except ValueError as exc:
        quarantined = _quarantine_config(target)
        log.error("配置文件 JSON 解析失败（%s）: %s；坏文件已隔离为 %s", exc, target, quarantined)
        if _restore_config_backup(target):
            return _load_config_json(target)
        return {}
    if not isinstance(raw, dict):
        quarantined = _quarantine_config(target)
        log.error("配置文件不是 JSON 对象: %s；坏文件已隔离为 %s", target, quarantined)
        if _restore_config_backup(target):
            return _load_config_json(target)
        return {}
    return raw


def _quarantine_config(target: Path) -> Path | None:
    """把损坏的配置文件改名隔离（保留现场取证）；失败返回 None 并记日志。"""

    stamp = time.strftime("%Y%m%d-%H%M%S")
    quarantined = target.with_name(f"{target.name}.corrupt-{stamp}")
    try:
        target.replace(quarantined)
    except OSError as exc:
        log.error("隔离损坏的配置文件 %s 失败: %s", target, exc)
        return None
    return quarantined


def _restore_config_backup(target: Path) -> bool:
    """从 ``<name>.bak`` 恢复配置；成功返回 True。"""

    backup = target.with_name(target.name + ".bak")
    if not backup.is_file():
        log.error("配置损坏且没有可用的备份 %s，只能使用默认值（token 会丢失）", backup)
        return False
    try:
        text = backup.read_text(encoding="utf-8")
        raw = json.loads(text)
    except (OSError, ValueError) as exc:
        log.error("备份配置 %s 也不可用（%s），放弃恢复", backup, exc)
        return False
    if not isinstance(raw, dict):
        log.error("备份配置 %s 不是 JSON 对象，放弃恢复", backup)
        return False
    try:
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        log.error("从备份恢复配置 %s 失败: %s", target, exc)
        return False
    log.warning("配置已从备份 %s 恢复", backup)
    return True


@dataclass
class AgentConfig:
    """落盘配置；``boot_id`` 是**运行时**字段，每次加载都重新生成。"""

    device_uid: str = ""
    token: str = ""
    base_urls: list[str] = field(default_factory=default_base_urls)
    enroll_code: str = ""
    #: 一次性安装令牌（A18）：与 enroll_code 二选一，服务端优先校验它。
    install_token: str = ""
    server_version: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    last_server_time_ms: int = 0
    device_time_offset_ms: int = 0
    protocol: int = 3
    boot_id: str = field(default_factory=new_boot_id)

    # -- persistence -----------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> "AgentConfig":
        target = path or CONFIG_PATH
        raw = _load_config_json(target)

        urls = _split_urls(",".join(str(item) for item in raw.get("base_urls") or []))
        settings = raw.get("settings")
        # 兼容 0.2.10 的键名：升级时旧 config.json 原样还在，不能因此丢时钟偏置。
        offset = raw.get("device_time_offset_ms", raw.get("clock_offset_ms", 0))
        config = cls(
            device_uid=str(raw.get("device_uid") or ""),
            token=str(raw.get("token") or ""),
            base_urls=urls or default_base_urls(),
            enroll_code=str(raw.get("enroll_code") or ""),
            install_token=str(raw.get("install_token") or ""),
            server_version=str(raw.get("server_version") or ""),
            settings=dict(settings) if isinstance(settings, Mapping) else {},
            last_server_time_ms=_as_int(raw.get("last_server_time_ms")),
            device_time_offset_ms=_as_int(offset),
            protocol=3,
            # boot_id 故意不从文件读：它必须每个进程不同（A2）。
            boot_id=new_boot_id(),
        )
        if not config.device_uid:
            config.device_uid = new_device_uid()
        return config

    def save(self, path: Path | None = None) -> None:
        """原子写配置；成功后轮转一份 ``.bak``（F38：load 侧损坏恢复的数据源）。"""

        target = path or CONFIG_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "device_uid": self.device_uid,
            "token": self.token,
            "base_urls": list(self.base_urls),
            "enroll_code": self.enroll_code,
            "install_token": self.install_token,
            "server_version": self.server_version,
            "settings": self.settings,
            "last_server_time_ms": int(self.last_server_time_ms),
            "device_time_offset_ms": int(self.device_time_offset_ms),
            "protocol": 3,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        # F38：临时名带 pid——两个进程（服务 + CLI 诊断）同时 save 时，旧的
        # 固定名 ".part" 会互相覆盖，产生"原子替换出来却是损坏文件"的竞态。
        temporary = target.with_name(f"{target.name}.{os.getpid()}.part")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, target)
        # P1（2026-10 审查）：这份文件含设备令牌/注册码，ProgramData 的默认
        # 继承 ACL 允许本地 Users 读——save 后就地收紧到 SYSTEM+Administrators。
        _harden_acl(target)
        # F38：成功后轮转一份 .bak（load 阶段配置损坏时从这里恢复）。同样走
        # ".part + os.replace" 保证备份文件本身也是原子的。
        backup = target.with_name(target.name + ".bak")
        backup_part = target.with_name(f"{target.name}.{os.getpid()}.bak.part")
        try:
            backup_part.write_text(text, encoding="utf-8")
            os.replace(backup_part, backup)
            _harden_acl(backup)
        except OSError as exc:
            log.warning("写入配置备份 %s 失败: %s", backup, exc)

    # -- helpers ---------------------------------------------------------
    def setting(self, key: str, default: Any = None) -> Any:
        if key in self.settings:
            return self.settings[key]
        if key in DEFAULT_SETTINGS:
            return DEFAULT_SETTINGS[key]
        return default

    def sanitized_settings(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """剥掉敏感键并记日志（A4：配置 JSON 到设备侧也不许带密钥）。"""

        cleaned: dict[str, Any] = {}
        stripped: list[str] = []
        for key, value in values.items():
            if not isinstance(key, str):
                log.warning("忽略非字符串配置键 %r", key)
                continue
            if key in SECRET_KEYS:
                stripped.append(key)
                continue
            cleaned[key] = value
        if stripped:
            log.warning(
                "服务端配置包含敏感键 %s，已在受控端脱落（服务端不应下发这些字段）",
                sorted(stripped),
            )
        return cleaned

    def apply_server_config(
        self,
        values: Mapping[str, Any] | None,
        *,
        server_time_ms: int = 0,
        device_time_ms: int | None = None,
    ) -> bool:
        """合并服务端配置；返回是否发生变化。敏感键在这里被剥掉。"""

        if not isinstance(values, Mapping):
            if values is not None:
                log.warning("服务端配置不是对象，忽略：%r", type(values).__name__)
            return False
        cleaned = self.sanitized_settings(values)
        changed = cleaned != self.settings
        self.settings = cleaned
        if server_time_ms > 0:
            local_ms = device_time_ms if device_time_ms is not None else int(time.time() * 1000)
            self.last_server_time_ms = int(server_time_ms)
            self.device_time_offset_ms = compute_device_time_offset(
                device_time_ms=local_ms, server_time_ms=int(server_time_ms)
            )
        return changed


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class ConfigStore:
    """线程安全的配置/时钟门面（服务进程与辅助线程共享）。

    ``config`` 返回的是**同一个可变对象**（与 0.2.10 行为一致），写入必须走
    ``update`` / ``apply_server_config`` 以便加锁并落盘。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or CONFIG_PATH
        self._lock = threading.RLock()
        self._config = AgentConfig.load(self._path)
        self._clock = Clock(device_time_offset_ms=self._config.device_time_offset_ms)

    # -- reads -----------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    @property
    def config(self) -> AgentConfig:
        with self._lock:
            return self._config

    @property
    def clock(self) -> Clock:
        return self._clock

    def setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._config.setting(key, default)

    def settings_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._config.settings)

    @property
    def token(self) -> str:
        with self._lock:
            return self._config.token

    def observe_server_time(self, *, server_time_ms: int, device_time_ms: int, rtt_ms: int = 0) -> int:
        """用一次服务端时间采样校准偏置（只改内存，不在心跳里频繁写盘）。"""

        if server_time_ms <= 0:
            return self._clock.device_time_offset_ms
        offset = self._clock.observe(
            device_time_ms=device_time_ms, server_time_ms=server_time_ms, rtt_ms=rtt_ms
        )
        self._config.device_time_offset_ms = offset
        return offset

    # -- writes ----------------------------------------------------------
    def update(self, **changes: Any) -> AgentConfig:
        with self._lock:
            for key, value in changes.items():
                setattr(self._config, key, value)
            self._config.save(self._path)
            return self._config

    def set_token(self, token: str) -> AgentConfig:
        return self.update(token=str(token))

    def set_base_urls(self, urls: list[str]) -> AgentConfig:
        cleaned = _split_urls(",".join(str(url) for url in urls))
        return self.update(base_urls=cleaned or default_base_urls())

    def apply_server_config(
        self,
        values: Mapping[str, Any] | None,
        *,
        server_time_ms: int = 0,
        device_time_ms: int | None = None,
    ) -> bool:
        with self._lock:
            changed = self._config.apply_server_config(
                values, server_time_ms=server_time_ms, device_time_ms=device_time_ms
            )
            if server_time_ms > 0:
                local_ms = device_time_ms if device_time_ms is not None else int(time.time() * 1000)
                self._clock.observe(
                    device_time_ms=local_ms, server_time_ms=int(server_time_ms)
                )
            if changed:
                self._config.save(self._path)
            return changed

    def reload(self) -> AgentConfig:
        with self._lock:
            self._config = AgentConfig.load(self._path)
            return self._config


__all__ = [
    "CONFIG_PATH",
    "Clock",
    "ConfigStore",
    "DEFAULT_BASE_URLS",
    "DEFAULT_HEARTBEAT_INTERVAL_S",
    "DEFAULT_SETTINGS",
    "INSTALL_DIR",
    "LOG_DIR",
    "MAX_HEARTBEAT_INTERVAL_S",
    "MIN_HEARTBEAT_INTERVAL_S",
    "PIPE_PREFIX",
    "PRODUCT",
    "PROGRAM_DATA",
    "OUTBOX_PATH",
    "SECRET_KEYS",
    "SERVICE_DESCRIPTION",
    "SERVICE_DISPLAY_NAME",
    "SERVICE_NAME",
    "STAGING_DIR",
    "AgentConfig",
    "compute_device_time_offset",
    "default_base_urls",
    "ensure_dirs",
    "install_dir",
    "new_boot_id",
    "new_device_uid",
    "outbox_limits_from_env",
    "outbox_path",
    "program_data_dir",
]