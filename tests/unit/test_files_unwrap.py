"""files 载荷解包回归（2026-10-25 生产：驱动器/目录列表恒空、读文件恒报无法解码）。

受控端 files 结果会带 1-2 层 ``{ok, op, data{...}}`` 包装；服务端必须剥到
载荷本体。形状取自当日生产实测响应。
"""

from observatory.console.files import _unwrap_agent_payload

DRIVE = {
    "name": "本地磁盘 (C:)",
    "path": "C:\\",
    "is_dir": True,
    "size": 53684989952,
    "mtime_ms": 0,
    "hidden": False,
    "drive": "C:",
    "free": 42551586816,
    "filesystem": "NTFS",
}


def test_unwraps_double_wrapped_drives() -> None:
    # 生产实测形状：perform() 一层 + 管道 CommandResult 一层
    payload = {
        "ok": True,
        "op": "drives",
        "data": {
            "ok": True,
            "op": "drives",
            "data": {"drives": [DRIVE], "server_time_ms": 1},
            "error": None,
            "elapsed_ms": 3,
        },
        "error": None,
        "elapsed_ms": 295,
    }
    unwrapped = _unwrap_agent_payload(payload)
    assert unwrapped["drives"] == [DRIVE]


def test_unwraps_single_wrapped_list() -> None:
    payload = {
        "ok": True,
        "op": "list",
        "data": {"path": "C:\\", "parent": None, "entries": [], "truncated": False, "drives": None},
    }
    unwrapped = _unwrap_agent_payload(payload)
    assert unwrapped["path"] == "C:\\"
    assert unwrapped["entries"] == []


def test_read_chunk_payload_is_not_unwrapped() -> None:
    # read 的 data 字段是 base64 字符串：不是 dict，循环立即停止，不得误剥
    payload = {"path": "C:\\a.bin", "data": "QUJD", "size": 3, "offset": 0, "eof": True}
    assert _unwrap_agent_payload(payload) is payload


def test_plain_payload_passthrough() -> None:
    payload = {"path": "C:\\a.bin", "sha256": "ab" * 32, "size": 1}
    assert _unwrap_agent_payload(payload) is payload
