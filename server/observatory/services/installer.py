"""Installer artifacts: the PowerShell script and the double-clickable wrapper.

The console hands out **files**, not command lines: an operator downloads
``Observatory-Install-<version>.cmd`` and double-clicks it. Everything it needs is
already inside — enroll code, version, and the list of server addresses to try in
order, so a machine that cannot reach one domain still installs from the other.

Rendering per request (rather than baking a file on disk) means rotating
``OBS_ENROLL_CODE`` or adding a domain takes effect immediately.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from ..models import now_ms
from .runtime import Runtime

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
PS1_NAME = "install.ps1"
CMD_NAME = "install.cmd"


def _read_template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8")


def _console_host(runtime: Runtime) -> str:
    hosts = runtime.settings.console_hosts
    return hosts[0] if hosts else ""


def render_ps1(runtime: Runtime, code: str) -> str:
    template = _read_template(PS1_NAME)
    quoted = ", ".join(f"'{url}'" for url in runtime.settings.base_urls)
    replacements = {
        "@@VERSION@@": runtime.version,
        "@@ENROLL_CODE@@": code,
        "@@BASE_URLS@@": quoted,
        "@@CONSOLE_HOST@@": _console_host(runtime),
        "@@SERVER_TIME_MS@@": str(now_ms()),
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


def render_cmd(runtime: Runtime, code: str) -> str:
    template = _read_template(CMD_NAME)
    url_lines = "\n".join(
        f'set "URL{index}={url}"' for index, url in enumerate(runtime.settings.base_urls, start=1)
    )
    replacements = {
        "@@VERSION@@": runtime.version,
        "@@ENROLL_CODE@@": code,
        # The code travels inside a query string; encode it so a custom code with
        # '+', '&' or '=' cannot corrupt the URL.
        "@@ENROLL_CODE_URL@@": quote(code, safe=""),
        "@@URL_LINES@@": url_lines,
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


def install_info(runtime: Runtime) -> dict:
    """What the console needs to render the download panel (admin-only endpoint)."""

    code = runtime.settings.enroll_code or ""
    return {
        "enabled": bool(code),
        "enroll_code": code,
        "version": runtime.version,
        "console_host": _console_host(runtime),
        "base_urls": list(runtime.settings.base_urls),
        "script_url": "/api/v1/public/install.ps1",
        "cmd_url": "/api/v1/public/install.cmd",
        "artifacts": [
            {
                "kind": "cmd",
                "label": "安装程序 (.cmd)",
                "filename": f"Observatory-Install-{runtime.version}.cmd",
                "url": f"/api/v1/public/install.cmd?code={quote(code, safe='')}" if code else "",
                "hint": "双击运行，内置注册码与多线路下载",
            },
            {
                "kind": "ps1",
                "label": "安装脚本 (.ps1)",
                "filename": f"Observatory-Install-{runtime.version}.ps1",
                "url": f"/api/v1/public/install.ps1?download=1&code={quote(code, safe='')}" if code else "",
                "hint": "给批量部署/组策略用，可传 -Code 覆盖",
            },
        ],
    }


__all__ = ["install_info", "render_cmd", "render_ps1"]