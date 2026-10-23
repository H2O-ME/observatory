"""0.2.10 受控端协议快照（冻结证据）。

WHY THIS FILE EXISTS
====================
0.3.0 重构会删除 `agent/observatory_agent/` 的 0.2.x 实现。N2（零重装升级）要求
证明「0.2.10 受控端仍能注册、收更新、校验、替换、并以新协议重连」，而那时旧代码
已不在仓库里。因此本文件把 0.2.10 **真实收发形状**逐字固化成常量（不 import 任何
`agent/` 或 `server/` 代码），作为唯一证据来源。

采集方式（0.2.10，VERSION=0.2.10）：
- 逐行阅读 `agent/observatory_agent/{client,transport,runner,worker,fs_ops,config,sysinfo}.py`
- 逐行阅读 `server/observatory/api/{agent,agent_ws}.py`、`schemas.py`、`framing.py`
- 与 `tools/test_server.py`、`tools/test_e2e_live.py` 中真实跑通过的 payload 交叉核对

冻结规则：
1. 这里只记录 0.2.10 **实际会发送/依赖** 的字段。服务端额外返回的字段不记录为
   旧端需求，但在 `SERVER_*` 常量里原样保留，避免测试把「旧端不读」误判成「可以删」。
2. 字段顺序无关，字段名与类型有关；测试用集合断言，不比较 JSON 文本。
3. 旧端对以下字段有硬依赖（删了就会坏，见 `LEGACY_HARD_REQUIREMENTS`）。

⚠️ 本文件是**只读证据**：任何 0.3.0 的协议变更都不得修改这里，只能新增文件
（例如 `agent_0_3_0.py`）并让 `tests/contract/test_legacy_v2.py` 同时回放两套。
"""

from __future__ import annotations

import json
import struct

# ---------------------------------------------------------------------------
# 0. 标识
# ---------------------------------------------------------------------------

LEGACY_PROTOCOL_ID = "observatory-agent/0.2.x"
LEGACY_AGENT_VERSION = "0.2.10"
LEGACY_SERVER_ACCEPTS_VERSIONS = ("0.2.5", "0.2.6", "0.2.7", "0.2.8", "0.2.9", "0.2.10")

#: 0.2.10 的 HTTP 设备令牌头（WS 也用它，见 client.py:91 与 deps.py:118-122）
DEVICE_TOKEN_HEADER = "X-Device-Token"

#: 0.2.10 的端点全集（spec §2.3 要求 0.3.0 继续提供）
LEGACY_ENDPOINTS = {
    "enroll": ("POST", "/api/v1/agent/enroll"),
    "heartbeat": ("POST", "/api/v1/agent/heartbeat"),
    "poll_commands": ("GET", "/api/v1/agent/commands?wait=<0..poll_hold_s>"),
    "command_result": ("POST", "/api/v1/agent/commands/{command_id}/result"),
    "frame_upload": ("POST", "/api/v1/agent/frames?ts_ms=<int>&session_id=<int>&kind=screen"),
    "device_state": ("GET", "/api/v1/agent/state"),
    "uninstall_ack": ("POST", "/api/v1/agent/uninstall/ack"),
    "agent_ws": ("WS", "/api/v1/agent/ws"),
    "public_package_download": ("GET", "/api/v1/public/agent/{version}/package"),
    "public_agent_latest": ("GET", "/api/v1/public/agent/latest"),
}


# ---------------------------------------------------------------------------
# 1. HTTP：注册（enroll）
# ---------------------------------------------------------------------------
# 发送方：client.py:148-163，payload 里 sessions = sysinfo.list_sessions()
# （sysinfo.py:168-205，含 session_id/username/state/is_console/station；
#   EnrollIn 未声明 station，但 pydantic 默认忽略多余键，旧端依赖这一点。）
# 接收方：api/agent.py:40-104；响应模型 schemas.py:379-387。

ENROLL_REQUEST = {
    "code": "test-enroll-code",
    "uid": "OBS-1A2B3C4D5E6F",
    "hostname": "PC-01",
    "os_name": "Windows",
    "os_version": "11 23H2",
    "arch": "AMD64",
    "agent_version": "0.2.10",
    "sysinfo": {
        "collected_at_ms": 1760000000000,
        "agent_version": "0.2.10",
        "hostname": "PC-01",
        "fqdn": "PC-01.local",
        "platform": "Windows",
        "os_name": "Windows",
        "os_release": "11",
        "os_version": "10.0.22631",
        "arch": "AMD64",
        "username": "operator",
        "cpu_percent": 12.5,
        "cpu_count": 8,
        "cpu_physical": 4,
        "cpu_mhz": 2400,
        "cpu_model": "Intel(R) Core(TM) i7",
        "mem_total": 8589934592,
        "mem_available": 4294967296,
        "mem_percent": 40.0,
        "swap_total": 0,
        "swap_percent": 0.0,
        "disks": [
            {
                "device": "C:\\",
                "mount": "C:\\",
                "fstype": "NTFS",
                "total": 511101108224,
                "used": 300000000000,
                "free": 211101108224,
                "percent": 58.7,
            }
        ],
        "sessions": [
            {
                "session_id": 1,
                "username": "operator",
                "state": "active",
                "is_console": True,
                "station": "Console",
            }
        ],
        "ips": ["192.168.1.10"],
        "boot_time_ms": 1759900000000,
        "uptime_s": 100000,
    },
    "sessions": [
        {
            "session_id": 1,
            "username": "operator",
            "state": "active",
            "is_console": True,
            "station": "Console",
        }
    ],
}

ENROLL_REQUEST_REQUIRED_KEYS = {
    "code",
    "uid",
    "hostname",
    "os_name",
    "os_version",
    "arch",
    "agent_version",
    "sysinfo",
    "sessions",
}

ENROLL_RESPONSE = {
    "device_uid": "OBS-1A2B3C4D5E6F",
    "token": "kZLqV9m2vQ7pXo1sJ8wYb3cR0dT5nH4eG6fA9uB2mC8",
    "config": {
        "capture_enabled": True,
        "capture_interval_s": 30,
        "capture_on_change_only": False,
        "capture_change_threshold": 8,
        "jpeg_quality": 72,
        "max_width": 1600,
        "thumb_width": 320,
        "ws_enabled": True,
        "poll_interval_s": 5,
        "transport_reprobe_s": 120,
        "control_enabled": True,
        "control_fps": 8,
        "control_quality": 55,
        "file_write_enabled": True,
        "file_upload_max_mb": 512,
        "auto_update": True,
        "heartbeat_interval_s": 20,
    },
    "base_urls": [
        "https://control.tianhw.top",
        "https://observatory.123423.xyz",
    ],
    "server_version": "0.2.10",
    "server_time_ms": 1760000000123,
    "heartbeat_interval_s": 20,
}

ENROLL_RESPONSE_REQUIRED_KEYS = {
    "device_uid",
    "token",
    "config",
    "base_urls",
    "server_version",
    "server_time_ms",
    "heartbeat_interval_s",
}


# ---------------------------------------------------------------------------
# 2. HTTP：心跳（heartbeat）
# ---------------------------------------------------------------------------
# 发送方：transport.py:193-229（PollChannel 每 heartbeat_s 调一次）
#          → client.py:168-174(POST /agent/heartbeat)
#          → runner.py:411-424(_heartbeat_payload)
# 接收方：api/agent.py:122-150；响应 schemas.py:395-403。

HEARTBEAT_REQUEST = {
    "agent_version": "0.2.10",
    "sysinfo": {
        "collected_at_ms": 1760000060000,
        "cpu_percent": 14.0,
        "cpu_count": 8,
        "cpu_physical": 4,
        "cpu_mhz": 2400,
        "cpu_model": "Intel(R) Core(TM) i7",
        "mem_total": 8589934592,
        "mem_available": 4294967296,
        "mem_percent": 41.0,
        "swap_total": 0,
        "swap_percent": 0.0,
        "uptime_s": 100060,
    },
    "state": {
        "transport": "long-poll",
        "connected": True,
        "sessions": [
            {
                "session_id": 1,
                "username": "operator",
                "state": "active",
                "is_console": True,
                "capturable": True,
            }
        ],
        "spool": 0,
        "control": [],
        "uptime_s": 3210,
        "updating": False,
        "capture": {
            "state": "ok",
            "reason": "",
            "workers": [1],
            "sessions": 1,
        },
    },
}

#: 0.2.10 可能上报的 state.transport 取值（transport.py:28-31 level_name）
LEGACY_TRANSPORT_VALUES = ("ws", "long-poll", "short-poll")

HEARTBEAT_RESPONSE = {
    "config": ENROLL_RESPONSE["config"],
    "server_time_ms": 1760000060123,
    "heartbeat_interval_s": 20,
    "poll_hold_s": 25,
    "update": {
        "version": "0.3.0",
        "url": "/api/v1/public/agent/0.3.0/package",
        "sha256": "9f2c1e0d8b7a6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d",
        "size_bytes": 19_500_000,
        "mandatory": False,
    },
    "uninstall": False,
    "commands": [],
    "acknowledged_uninstall": False,
}

HEARTBEAT_RESPONSE_REQUIRED_KEYS = {
    "config",
    "server_time_ms",
    "heartbeat_interval_s",
    "poll_hold_s",
    "update",
    "uninstall",
    "commands",
}


# ---------------------------------------------------------------------------
# 3. HTTP：命令轮询（long-poll / short-poll）
# ---------------------------------------------------------------------------
# 发送方：client.py:176-184（GET /agent/commands?wait=N；long=最多 20s，short=0）
# 接收方：api/agent.py:153-181。commands 元素是 commands.serialize() 的形状
#          （含 status，没有 timeout_ms —— 与 WS 下发不同，见 COMMAND_PUSH_WS）。

POLL_COMMAND_HIT = {
    "commands": [
        {
            "id": "9d1f0c2a4b6e48a7b3c5d7e9f1a2b4c6",
            "type": "files",
            "status": "dispatched",
            "payload": {"op": "list", "path": "C:\\Users"},
            "result": None,
            "error": None,
            "created_at_ms": 1760000061000,
            "dispatched_at_ms": 1760000062000,
            "finished_at_ms": None,
        }
    ],
    "server_time_ms": 1760000062123,
    "config": ENROLL_RESPONSE["config"],
    "update": None,
}

POLL_COMMAND_EMPTY = {
    "commands": [],
    "server_time_ms": 1760000082123,
    "config": ENROLL_RESPONSE["config"],
    "update": None,
}

POLL_RESPONSE_REQUIRED_KEYS = {"commands", "server_time_ms", "config", "update"}


# ---------------------------------------------------------------------------
# 4. HTTP：命令回执
# ---------------------------------------------------------------------------
# 发送方：client.py:186-196（POST /agent/commands/{id}/result）
# 接收方：api/agent.py:184-208；响应 OkOut（schemas.py:308-310）

COMMAND_RESULT_REQUEST_OK = {
    "ok": True,
    "result": {
        "ok": True,
        "op": "list",
        "data": {
            "path": "C:\\Users",
            "parent": "C:\\",
            "entries": [
                {
                    "name": "operator",
                    "path": "C:\\Users\\operator",
                    "is_dir": True,
                    "size": 0,
                    "mtime_ms": 1760000000000,
                    "hidden": False,
                    "drive": "C:",
                }
            ],
            "truncated": False,
            "drives": None,
            "server_time_ms": 1760000063000,
        },
        "error": None,
        "elapsed_ms": 12,
    },
    "error": None,
}

COMMAND_RESULT_REQUEST_FAIL = {
    "ok": False,
    "result": None,
    "error": "拒绝访问（5）",
}

COMMAND_RESULT_RESPONSE = {"ok": True, "message": "ok"}


# ---------------------------------------------------------------------------
# 5. HTTP：帧上传
# ---------------------------------------------------------------------------
# 发送方：client.py:198-210（POST /agent/frames?...，body 为原始 WebP 字节，
#         Content-Type: image/webp，超时 45s，连接错误会落到 spool 重传）
# 接收方：api/agent.py:211-241；响应 schemas.py:421-426。
# 注意：0.2.10 **没有** X-Frame-Id 幂等头；0.3.0 新增后必须对旧端保持可选。

FRAME_UPLOAD_QUERY = {"ts_ms": 1760000064000, "session_id": 1, "kind": "screen"}
FRAME_UPLOAD_HEADERS = {"Content-Type": "image/webp"}
#: 一个最小 RIFF/WEBP 容器头（真实帧是完整 WebP；测试只需字节形状）
FRAME_UPLOAD_BODY_PREFIX = b"RIFF$\x00\x00\x00WEBPVP8 "

FRAME_UPLOAD_RESPONSE = {
    "frame_id": 42,
    "ts_ms": 1760000064000,
    "stored": True,
    "server_time_ms": 1760000064123,
}

FRAME_UPLOAD_RESPONSE_REQUIRED_KEYS = {"frame_id", "ts_ms", "stored", "server_time_ms"}


# ---------------------------------------------------------------------------
# 6. HTTP：设备自视角 / 卸载确认（0.2.10 保留端点）
# ---------------------------------------------------------------------------
# device_state 端点存在但 0.2.10 runner 不调用（client.get_state 未被使用）；
# 仍属于 spec §2.3 要求保留的端点，故记录响应形状。

DEVICE_STATE_RESPONSE = {
    "presence": {
        "state": "degraded",
        "transport": "poll",
        "reason": "仅轮询通道，实时控制不可用",
        "last_seen_ms": 1760000065000,
        "age_ms": 123,
        "channel_since_ms": 1760000000000,
        "capabilities": {
            "control": False,
            "files": True,
            "screenshot": True,
            "timeline": True,
            "update": True,
            "uninstall": True,
        },
        "capture": {"state": "ok", "reason": "", "sessions": 1, "workers": [1], "at_ms": 1760000060000},
        "self_update_blocked": None,
    },
    "server_version": "0.2.10",
    "server_time_ms": 1760000065123,
    "base_urls": ENROLL_RESPONSE["base_urls"],
    "config": ENROLL_RESPONSE["config"],
}

UNINSTALL_ACK_REQUEST = {}
UNINSTALL_ACK_RESPONSE = {"ok": True, "message": "已记录卸载"}


# ---------------------------------------------------------------------------
# 7. HTTP 错误形状（0.2.10 客户端据此分支）
# ---------------------------------------------------------------------------
# 客户端行为：
# - 401 heartbeat/poll：PermissionError → runner._reregister() → 清 token → 重新 enroll
#   （client.py:168-184、runner.py:389-392）
# - enroll 非 200：TransportError（client.py:164-165），保持未注册并 15s 后重试
# - 410：设备已卸载（deps.py:91-92）；0.2.10 未显式处理，会当作 TransportError 重试

HTTP_ERROR_SHAPES = {
    "missing_device_token": {"status": 401, "body": {"detail": "缺少设备令牌"}},
    "invalid_device_token": {"status": 401, "body": {"detail": "设备令牌无效"}},
    "uninstalled": {"status": 410, "body": {"detail": "该设备已卸载"}},
    "enroll_bad_code": {"status": 401, "body": {"detail": "注册码不正确"}},
    "enroll_disabled": {"status": 403, "body": {"detail": "服务器未开启注册（OBS_ENROLL_CODE 为空）"}},
    "enroll_rate_limited": {"status": 429, "body": {"detail": "注册过于频繁，请稍后再试"}},
    "enroll_bad_uid": {"status": 400, "body": {"detail": "设备标识不合法"}},
    "command_not_found": {"status": 404, "body": {"detail": "命令不存在"}},
    "frame_too_large": {"status": 413, "body": {"detail": "图像过大"}},
    "frame_undecodable": {"status": 422, "body": {"detail": "图像无法解码"}},
}


# ---------------------------------------------------------------------------
# 8. WebSocket 消息（app 层 JSON）
# ---------------------------------------------------------------------------
# 连接：wss://<host>/api/v1/agent/ws，请求头 X-Device-Token（client.py:246-252、
# transport.py:84-93；服务端 ws_device_token 也接受 ?token=，见 deps.py:118-122）
# 服务端：api/agent_ws.py。0.2.10 只在 WS 上收欢迎/ping/命令/控制，发心跳/回执/事件/控制帧。

WS_URL_PATH = "/api/v1/agent/ws"

#: 服务端关闭码（api/agent_ws.py:39-41、services/hub.py:80）
WS_CLOSE_CODES = {
    4401: "invalid device token",
    4002: "device uninstalled / gone",
    4408: "idle timeout",
    4000: "replaced by newer connection",
}

WS_SERVER_WELCOME = {
    "t": "welcome",
    "device_uid": "OBS-1A2B3C4D5E6F",
    "server_version": "0.2.10",
    "server_time_ms": 1760000000123,
    "heartbeat_interval_s": 20,
    "config": ENROLL_RESPONSE["config"],
    "update": None,
    "poll_hold_s": 25,
}
WS_SERVER_WELCOME_REQUIRED_KEYS = {
    "t",
    "device_uid",
    "server_version",
    "server_time_ms",
    "heartbeat_interval_s",
    "config",
    "update",
    "poll_hold_s",
}

WS_SERVER_PING = {"t": "ping", "ts": 1760000005000}

WS_SERVER_PONG = {
    "t": "pong",
    "server_time_ms": 1760000060123,
    "config": ENROLL_RESPONSE["config"],
    "update": None,
    "heartbeat_interval_s": 20,
}

WS_SERVER_CONFIG = {
    "t": "config",
    "config": ENROLL_RESPONSE["config"],
    "server_time_ms": 1760000006000,
}

#: 服务端经 WS 下发的命令：带 timeout_ms、不带 status（与 POLL 的命令对象不同！）
WS_SERVER_COMMAND = {
    "t": "command",
    "command": {
        "id": "9d1f0c2a4b6e48a7b3c5d7e9f1a2b4c6",
        "type": "files",
        "payload": {"op": "list", "path": "C:\\Users"},
        "timeout_ms": 90000,
    },
}

WS_SERVER_CTRL_START = {"t": "ctrl_start", "session": "grant-token", "fps": 8, "quality": 55, "ttl_s": 300}
WS_SERVER_CTRL_IN = {"t": "ctrl_in", "session": "grant-token", "ev": {"kind": "mouse", "action": "move", "x": 5000, "y": 5000}}
WS_SERVER_CTRL_STOP = {"t": "ctrl_stop", "session": "grant-token"}

#: 0.2.10 → 服务端
WS_CLIENT_HEARTBEAT = {"t": "heartbeat", **HEARTBEAT_REQUEST}

WS_CLIENT_RESULT_OK = {
    "t": "result",
    "command_id": "9d1f0c2a4b6e48a7b3c5d7e9f1a2b4c6",
    "ok": True,
    "result": COMMAND_RESULT_REQUEST_OK["result"],
    "error": None,
}

WS_CLIENT_RESULT_FAIL = {
    "t": "result",
    "command_id": "9d1f0c2a4b6e48a7b3c5d7e9f1a2b4c6",
    "ok": False,
    "result": None,
    "error": "拒绝访问（5）",
}

WS_CLIENT_EVENT = {"t": "event", "event": "update_started", "data": {"version": "0.3.0"}}
WS_CLIENT_PONG = {"t": "pong", "ts": 1760000005123}
WS_CLIENT_UNINSTALL_ACK = {"t": "uninstall_ack"}

#: 0.2.10 会发送的事件名（runner.py:_report_state / _maybe_update / _begin_uninstall）
LEGACY_AGENT_EVENTS = (
    "transport",       # {"stage": "connected", "level": "ws"|"long-poll"|"short-poll", "connected": bool}
    "update_started",  # {"version": ...}
    "update_downloading",
    "update_unpacking",
    "update_ready",
    "update_failed",   # {"version", "error", "attempt"}
    "uninstall_started",
)


# ---------------------------------------------------------------------------
# 9. 二进制帧（仅 WS 载体的控制画面）
# ---------------------------------------------------------------------------
# 格式：uint32 big-endian 头长 | UTF-8 JSON 头 | payload
# （agent pipe_proto.py:25-52 与 server framing.py:20-45 同构）
# 0.2.10 只在上行控制帧里使用它；画面帧（screen）始终走 HTTP（runner.py:988-1003）。

CTRL_FRAME_HEADER = {
    "t": "ctrl",
    "session": "grant-token",
    "session_id": 1,
    "ts": 1760000067000,
    "w": 1600,
    "h": 900,
    "len": 8,
}
CTRL_FRAME_PAYLOAD = b"RIFF\x00\x00\x00\x00WEBP"
MAX_HEADER_BYTES = 64 * 1024
HEADER_STRUCT = struct.Struct(">I")


def encode_binary_frame(header: dict, payload: bytes = b"") -> bytes:
    """按 0.2.10 的 wire format 编码（独立实现，避免依赖被删的旧模块）。"""

    raw = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_HEADER_BYTES:
        raise ValueError("header too large")
    return HEADER_STRUCT.pack(len(raw)) + raw + payload


def decode_binary_frame(blob: bytes) -> tuple[dict, bytes]:
    if len(blob) < HEADER_STRUCT.size:
        raise ValueError("truncated header length")
    (length,) = HEADER_STRUCT.unpack_from(blob, 0)
    if length <= 0 or length > MAX_HEADER_BYTES:
        raise ValueError(f"invalid header length {length}")
    start = HEADER_STRUCT.size
    end = start + length
    if len(blob) < end:
        raise ValueError("truncated header")
    header = json.loads(blob[start:end].decode("utf-8"))
    if not isinstance(header, dict):
        raise ValueError("header must be an object")
    return header, blob[end:]


# ---------------------------------------------------------------------------
# 10. 真实会话序列（用于回放）
# ---------------------------------------------------------------------------
# 每条 = (方向, 消息)；方向 "c2s" / "s2c"。这些序列本身就是 N2 的验收脚本骨架。

WS_SESSION_SEQUENCE = (
    ("c2s", {"__http_upgrade__": True, "path": WS_URL_PATH, "headers": {DEVICE_TOKEN_HEADER: "<token>"}}),
    ("s2c", WS_SERVER_WELCOME),
    ("s2c", WS_SERVER_PING),
    ("c2s", WS_CLIENT_PONG),
    ("c2s", WS_CLIENT_HEARTBEAT),
    ("s2c", WS_SERVER_PONG),
    ("s2c", WS_SERVER_COMMAND),
    ("c2s", WS_CLIENT_RESULT_OK),
    ("c2s", WS_CLIENT_EVENT),
)

POLL_SESSION_SEQUENCE = (
    ("c2s", {"method": "POST", "path": "/api/v1/agent/heartbeat", "json": HEARTBEAT_REQUEST}),
    ("s2c", HEARTBEAT_RESPONSE),
    ("c2s", {"method": "GET", "path": "/api/v1/agent/commands", "query": {"wait": 20}}),
    ("s2c", POLL_COMMAND_HIT),
    ("c2s", {"method": "POST", "path": "/api/v1/agent/commands/{id}/result", "json": COMMAND_RESULT_REQUEST_OK}),
    ("s2c", {"status": 200, "json": COMMAND_RESULT_RESPONSE}),
    ("c2s", {"method": "GET", "path": "/api/v1/agent/commands", "query": {"wait": 20}}),
    ("s2c", POLL_COMMAND_EMPTY),
)


# ---------------------------------------------------------------------------
# 11. N2：零重装升级剧本（0.2.10 → 0.3.0）
# ---------------------------------------------------------------------------
# 这是测试要证明的行为链；old agent 端的每一步都有代码位置注释。
# 注意两个 0.2.10 的真实怪癖，服务端必须继续容忍：
#   1) 旧端**忽略** update.url，自己拼 `/api/v1/public/agent/<version>/package`
#      （client.py:218-238），所以该路径必须继续可下载。
#   2) 旧端在 sha256 为空时**跳过校验**（client.py:234）。0.3.0 服务端必须始终下发
#      非空 sha256，否则旧端会静默安装未校验的包。
N2_UPGRADE_SCENARIO = {
    "name": "n2-zero-reinstall-0.2.10-to-0.3.0",
    "given": {
        "device_uid": "OBS-1A2B3C4D5E6F",
        "agent_version": "0.2.10",
        "transport": "ws",
        "server_version": "0.3.0",
        "update_offer": {
            "version": "0.3.0",
            "url": "/api/v1/public/agent/0.3.0/package",
            "sha256": HEARTBEAT_RESPONSE["update"]["sha256"],
            "size_bytes": 19_500_000,
            "mandatory": False,
        },
    },
    "steps": (
        # 1) 旧端继续用旧协议跑（证明兼容层存在）
        "legacy_enroll_accepted",
        "legacy_heartbeat_or_poll_accepted",
        # 2) 服务端在 welcome/pong/heartbeat 的 update 字段里提供 0.3.0（非空 sha256）
        "legacy_receives_update_offer",
        # 3) 旧端自行拼 /api/v1/public/agent/0.3.0/package 下载，流式算 sha256 并比对
        "legacy_downloads_and_verifies_sha256",
        # 4) 解包 → 写 VERSION → 预检 `observatory-agent.exe --version` == 0.3.0
        "legacy_stages_and_preflights_new_build",
        # 5) 旧服务停止 → helper 原子替换安装目录 → 启动服务（失败回滚）
        "legacy_helper_swaps_and_starts_service",
        # 6) 新 0.3.0 受控端以 v3 信封重连；服务端记录 protocol=v3、agent_version=0.3.0
        "new_agent_reconnects_with_v3_envelope",
        # 7) 控制台看到版本变化，且无需重装（install 目录未整体重建、设备 uid 不变）
        "console_reports_0_3_0_without_reinstall",
    ),
    "invariants": (
        "device_uid unchanged",
        "enrollment token unchanged (no re-enroll needed)",
        "no UAC prompt (SYSTEM helper)",
        "old endpoint set still served until 0.4",
    ),
}


# ---------------------------------------------------------------------------
# 12. 服务端对旧端的硬性承诺（删任一项都会破坏 N2）
# ---------------------------------------------------------------------------
LEGACY_HARD_REQUIREMENTS = {
    "endpoints": tuple(key for key in LEGACY_ENDPOINTS),
    "request_headers": (DEVICE_TOKEN_HEADER,),
    "ws_handshake_headers": (DEVICE_TOKEN_HEADER,),
    "response_fields": {
        "enroll": ENROLL_RESPONSE_REQUIRED_KEYS,
        "heartbeat": HEARTBEAT_RESPONSE_REQUIRED_KEYS,
        "poll": POLL_RESPONSE_REQUIRED_KEYS,
        "result": {"ok", "message"},
        "frame_upload": FRAME_UPLOAD_RESPONSE_REQUIRED_KEYS,
        "ws_welcome": WS_SERVER_WELCOME_REQUIRED_KEYS,
    },
    "update_offer_fields": {"version", "url", "sha256", "size_bytes"},
    "config_keys_read_by_old_agent": (
        "heartbeat_interval_s",
        "auto_update",
        "control_enabled",
        "capture_enabled",
        "capture_interval_s",
        "capture_on_change_only",
        "capture_change_threshold",
        "max_width",
        "jpeg_quality",
        "file_write_enabled",
        "control_fps",
        "control_quality",
    ),
    "close_codes": tuple(WS_CLOSE_CODES),
    "client_failure_behaviour": {
        "401": "clear token and re-enroll",
        "enroll_non_200": "retry after 15s, stay unenrolled",
        "poll_hold": "server returns ≤ poll_hold_s; old agent waits wait_s",
    },
}


# ---------------------------------------------------------------------------
# 13. 派生辅助（供回放测试构造请求，不改变上面的冻结值）
# ---------------------------------------------------------------------------
def enroll_request(**overrides) -> dict:
    payload = json.loads(json.dumps(ENROLL_REQUEST))
    payload.update(overrides)
    return payload


def heartbeat_request(**state_overrides) -> dict:
    payload = json.loads(json.dumps(HEARTBEAT_REQUEST))
    payload["state"].update(state_overrides)
    return payload


def command_result(command_id: str, *, ok: bool = True, result: dict | None = None, error: str | None = None) -> dict:
    return {"ok": ok, "result": result, "error": error}


def ws_result(command_id: str, *, ok: bool = True, result: dict | None = None, error: str | None = None) -> dict:
    return {
        "t": "result",
        "command_id": command_id,
        "ok": ok,
        "result": result,
        "error": error,
    }


def update_offer(version: str = "0.3.0", sha256: str | None = None) -> dict:
    return {
        "version": version,
        "url": f"/api/v1/public/agent/{version}/package",
        "sha256": sha256 if sha256 is not None else HEARTBEAT_RESPONSE["update"]["sha256"],
        "size_bytes": 19_500_000,
        "mandatory": False,
    }


def required_keys_of(payload: dict) -> set[str]:
    return set(payload)


__all__ = [
    "COMMAND_RESULT_REQUEST_FAIL",
    "COMMAND_RESULT_REQUEST_OK",
    "COMMAND_RESULT_RESPONSE",
    "CTRL_FRAME_HEADER",
    "CTRL_FRAME_PAYLOAD",
    "DEVICE_TOKEN_HEADER",
    "DEVICE_STATE_RESPONSE",
    "ENROLL_REQUEST",
    "ENROLL_REQUEST_REQUIRED_KEYS",
    "ENROLL_RESPONSE",
    "ENROLL_RESPONSE_REQUIRED_KEYS",
    "FRAME_UPLOAD_BODY_PREFIX",
    "FRAME_UPLOAD_HEADERS",
    "FRAME_UPLOAD_QUERY",
    "FRAME_UPLOAD_RESPONSE",
    "FRAME_UPLOAD_RESPONSE_REQUIRED_KEYS",
    "HEARTBEAT_REQUEST",
    "HEARTBEAT_RESPONSE",
    "HEARTBEAT_RESPONSE_REQUIRED_KEYS",
    "HTTP_ERROR_SHAPES",
    "LEGACY_AGENT_EVENTS",
    "LEGACY_AGENT_VERSION",
    "LEGACY_ENDPOINTS",
    "LEGACY_HARD_REQUIREMENTS",
    "LEGACY_PROTOCOL_ID",
    "LEGACY_SERVER_ACCEPTS_VERSIONS",
    "LEGACY_TRANSPORT_VALUES",
    "N2_UPGRADE_SCENARIO",
    "POLL_COMMAND_EMPTY",
    "POLL_COMMAND_HIT",
    "POLL_RESPONSE_REQUIRED_KEYS",
    "POLL_SESSION_SEQUENCE",
    "UNINSTALL_ACK_REQUEST",
    "UNINSTALL_ACK_RESPONSE",
    "WS_CLIENT_EVENT",
    "WS_CLIENT_HEARTBEAT",
    "WS_CLIENT_PONG",
    "WS_CLIENT_RESULT_FAIL",
    "WS_CLIENT_RESULT_OK",
    "WS_CLIENT_UNINSTALL_ACK",
    "WS_CLOSE_CODES",
    "WS_SERVER_COMMAND",
    "WS_SERVER_CONFIG",
    "WS_SERVER_CTRL_IN",
    "WS_SERVER_CTRL_START",
    "WS_SERVER_CTRL_STOP",
    "WS_SERVER_PING",
    "WS_SERVER_PONG",
    "WS_SERVER_WELCOME",
    "WS_SERVER_WELCOME_REQUIRED_KEYS",
    "WS_SESSION_SEQUENCE",
    "WS_URL_PATH",
    "command_result",
    "decode_binary_frame",
    "encode_binary_frame",
    "enroll_request",
    "heartbeat_request",
    "required_keys_of",
    "update_offer",
    "ws_result",
]