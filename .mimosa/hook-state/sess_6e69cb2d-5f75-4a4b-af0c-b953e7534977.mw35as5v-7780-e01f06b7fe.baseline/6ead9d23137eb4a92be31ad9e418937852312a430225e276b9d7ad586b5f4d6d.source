"""出站 DTO：配置脱敏（A4）与时钟单一来源（A12）。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from observatory.console.dto import device_safe_config, file_entry_out, stamp
from observatory.core.clock import now_ms
from observatory.core.settings_spec import AGENT_VISIBLE_KEYS, SECRET_KEYS

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WEBUI_SRC = REPO_ROOT / "webui/src"


def _strip_ts_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def test_device_safe_config_strips_every_secret_key() -> None:
    values = {key: "should-never-leak" for key in SECRET_KEYS}
    values["capture_interval_s"] = 30
    safe = device_safe_config(values)
    assert not (set(safe) & SECRET_KEYS), f"泄漏: {set(safe) & SECRET_KEYS}"
    assert safe["capture_interval_s"] == 30
    assert set(safe) <= AGENT_VISIBLE_KEYS
    assert AGENT_VISIBLE_KEYS.isdisjoint(SECRET_KEYS)


def test_stamp_carries_server_clock() -> None:
    before = now_ms()
    stamped = stamp({"ok": True})
    after = now_ms()
    assert before <= stamped["server_time_ms"] <= after


def test_file_entry_renames_device_time_and_never_server_time() -> None:
    entry = file_entry_out({"name": "a.txt", "path": "C:/a.txt", "mtime_ms": 123})
    assert entry["device_time_ms"] == 123
    assert "mtime_ms" not in entry
    assert "server_time_ms" not in entry, "受控端时间不能叫 server_time_ms（C5/A12）"


def test_webui_code_never_uses_date_now() -> None:
    offenders: list[str] = []
    for path in WEBUI_SRC.rglob("*"):
        if path.suffix not in {".ts", ".tsx"}:
            continue
        code = _strip_ts_comments(path.read_text(encoding="utf-8"))
        if "Date.now(" in code:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"前端代码不能再用 Date.now() 判新鲜度: {offenders}"


def test_outbound_config_paths_use_the_single_projection() -> None:
    """所有把 config 发给浏览器/受控端的路径都必须走 device_safe_config。"""

    server = REPO_ROOT / "server/observatory"
    for relative in (
        "console/settings.py",
        "console/devices.py",
        "console/enroll.py",
    ):
        text = (server / relative).read_text(encoding="utf-8")
        assert "device_safe_config" in text, f"{relative} 未走脱敏投影"


def test_console_files_dto_uses_server_clock_and_device_time_names() -> None:
    files = (REPO_ROOT / "server/observatory/console/files.py").read_text(encoding="utf-8")
    assert "stamp(" in files and "file_entry_out" in files
    # 受控端时间必须走 file_entry_out（重命名为 device_time_ms），不能直接塞进 server_time_ms
    assert "server_time_ms=" not in files