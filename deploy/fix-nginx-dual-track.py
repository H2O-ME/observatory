#!/usr/bin/env python3
"""Align the two host nginx vhosts with Observatory 0.2.0's dual-track contract.

Rules after this patch:

  control.tianhw.top        console  + agent API + install script  (both halves)
  observatory.123423.xyz    agent API + install script + version  (no console)

The old files refused the agent prefixes on the console host, which is the
opposite of the requirement: the two domains are supposed to back each other up
for installation and device traffic.

Idempotent: running it twice changes nothing.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

VHOSTS = Path("/www/server/panel/vhost/nginx")
CONSOLE = VHOSTS / "control.tianhw.top.conf"
AGENT = VHOSTS / "observatory.123423.xyz.conf"

CONSOLE_404_BLOCK = re.compile(
    r"\n[ \t]*location \^~ /api/v1/(?:agent|public)/ \{\n(?:[ \t]*#[^\n]*\n)*[ \t]*return 404;\n[ \t]*\}\n"
)

AGENT_WS_HEADERS = """        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 3600s;
"""

AGENT_EXTRA_LOCATIONS = """
    location = /api/v1/version {
        proxy_pass https://127.0.0.1:8443;
        proxy_ssl_verify off;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        access_log off;
    }

    # The one-line installer is served from both domains on purpose: a machine
    # that cannot reach one of them must still be able to install and report.
    location = /install.ps1 {
        proxy_pass https://127.0.0.1:8443;
        proxy_ssl_verify off;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
    }

"""


def backup(path: Path) -> Path:
    target = path.with_suffix(path.suffix + f".bak-observatory-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, target)
    return target


def patch_console(text: str) -> tuple[str, list[str]]:
    changes: list[str] = []
    if "/api/v1/public/ {\n        return 404;" in text or CONSOLE_404_BLOCK.search(text):
        text = CONSOLE_404_BLOCK.sub("\n", text)
        changes.append("允许控制台域名访问受控端接口（双域名互备）")
    return text, changes


def patch_agent(text: str) -> tuple[str, list[str]]:
    changes: list[str] = []

    # WebSocket support for /api/v1/agent/ws — without these headers nginx
    # downgrades the upgrade request and the agent can never leave long-poll.
    if "proxy_set_header Upgrade $http_upgrade;" not in text:
        agent_block = re.search(
            r"(location \^~ /api/v1/agent/ \{\n)(.*?)(\n[ \t]*\}\n)", text, re.DOTALL
        )
        if agent_block is None:
            raise SystemExit("找不到 /api/v1/agent/ 的 location 块")
        inner = agent_block.group(2)
        if "X-Forwarded-Host" in inner:
            inner = inner.replace(
                "        proxy_set_header X-Forwarded-Host $host;\n",
                AGENT_WS_HEADERS,
                1,
            )
        else:
            inner = inner + "\n" + AGENT_WS_HEADERS.rstrip("\n")
        text = text[: agent_block.start(2)] + inner + text[agent_block.end(2) :]
        changes.append("受控端域名支持 WebSocket 升级")

    if "location = /api/v1/version" not in text:
        marker = "\n    # --- everything else"
        if marker in text:
            text = text.replace(marker, "\n" + AGENT_EXTRA_LOCATIONS + marker[1:], 1)
        else:
            raise SystemExit("找不到插入点（everything else 注释）")
        changes.append("受控端域名提供 /api/v1/version 与 /install.ps1")

    return text, changes


def main() -> int:
    if not CONSOLE.exists() or not AGENT.exists():
        raise SystemExit("vhost 文件不存在")

    total: list[str] = []
    for path, patcher in ((CONSOLE, patch_console), (AGENT, patch_agent)):
        original = path.read_text(encoding="utf-8")
        updated, changes = patcher(original)
        if not changes:
            print(f"{path.name}: 已是最新")
            continue
        saved = backup(path)
        path.write_text(updated, encoding="utf-8")
        print(f"{path.name}: {', '.join(changes)} (备份 {saved.name})")
        total.extend(changes)

    if not total:
        return 0

    test = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
    if test.returncode != 0:
        print(test.stderr or test.stdout, file=sys.stderr)
        raise SystemExit("nginx 配置校验失败，未 reload")
    subprocess.run(["nginx", "-s", "reload"], check=True)
    print("nginx 已 reload")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())