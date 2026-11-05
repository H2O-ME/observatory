"""Agent 接入（0.3.19）：本地 AI agent 接管控制台能力的入口。

两个端点（都要求 console 认证）：
* ``GET /console/agent-skill/status`` — 页面用的轻量状态（api_key 是否已设置、
  版本、控制台地址）。secret 永不回显，只回布尔位。
* ``GET /console/agent-skill/download`` — 打包下载 skill（SKILL.md + scripts/obs.py）。
  模板在 ``templates/agent-skill/``，占位符只有版本号与控制台主机名，纯文本渲染，
  没有任何动态路径拼接。
"""

from __future__ import annotations

import io
import logging
import zipfile
from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from ..core.config import DEFAULT_CONSOLE_HOST, resolve_version
from ..core.db import get_session
from .deps import client_ip, require_admin, store
from .envelope import ConsoleRoute

try:  # SQLAlchemy 2 style AsyncSession (仅类型标注用)
    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession
except Exception:  # pragma: no cover
    _AsyncSession = Any  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["agent-skill"],
    dependencies=[Depends(require_admin)],
    route_class=ConsoleRoute,
)

_TEMPLATES = ("SKILL.md", "scripts/obs.py")


def _template_dir(request: Request):
    from pathlib import Path

    return Path(__file__).resolve().parents[1] / "templates" / "agent-skill"


def _console_host(request: Request) -> str:
    """与 system.py 的 bootstrap 同源：console_hosts 的第一项。"""

    settings = getattr(request.app.state, "settings", None)
    hosts = list(getattr(settings, "console_hosts", None) or [])
    return str(hosts[0]) if hosts else ""


def _render(text: str, *, version: str, console_host: str) -> str:
    return text.replace("@@VERSION@@", version).replace("@@CONSOLE_HOST@@", console_host)


@router.get("/console/agent-skill/status")
async def agent_skill_status(
    request: Request,
    session: _AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """页面状态：密钥是否已设置（不回显真值）+ 下载所需的静态信息。"""

    values = await store().load(session)
    return {
        "api_key_set": bool(str(values.get("api_key") or "").strip()),
        "exec_enabled_default": bool(values.get("exec_enabled", False)),
        "version": resolve_version(),
        "console_host": _console_host(request),
    }


@router.get("/console/agent-skill/download")
async def agent_skill_download(request: Request) -> Response:
    """打包 SKILL.md + scripts/obs.py；密钥等敏感信息不进包。"""

    from pathlib import Path

    version = resolve_version()
    console_host = _console_host(request) or DEFAULT_CONSOLE_HOST
    base_dir: Path = _template_dir(request)
    base_dir: Path = _template_dir(request)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for relative in _TEMPLATES:
            source = base_dir / relative
            text = source.read_text(encoding="utf-8")
            bundle.writestr(f"observatory-agent-skill/{relative}", _render(text, version=version, console_host=console_host))
        bundle.writestr(
            "observatory-agent-skill/README.txt",
            _render(
                "Observatory 本地 Agent 接入包（版本 @@VERSION@@）\n"
                "\n"
                "安装：把 observatory-agent-skill/ 整个目录放进你的 AI agent 的 skills\n"
                "目录——ZCode: ~/.zcode/skills/ 或项目 .zcode/skills/；Claude Code 等\n"
                "读 .agents/skills/ 的 agent 同理（实体目录放哪边、junction/软链指哪边\n"
                "都可以）。然后按其中的 SKILL.md 使用；scripts/obs.py 可独立当 CLI 用。\n"
                "\n"
                "环境变量（三个都可能需要）：\n"
                "  OBS_API_KEY         Bearer 密钥——控制台「Agent 接入」页生成\n"
                "  OBS_API_BASE        控制台地址，默认 https://@@CONSOLE_HOST@@\n"
                "  OBS_ALLOW_PRIVATE=1 本机代理 fake-ip DNS（Clash TUN 把域名解析成\n"
                "                      198.18.x.x）或内网控制台时必须设，否则 CLI 拒连\n"
                "  OBS_ALLOW_HTTP=1    仅本地 http 调试；默认强制 https\n"
                "\n"
                "命令行 --allow-private / --json 放在子命令前后都可以。\n"
                "退出码：0 成功；3 认证失败；4 远端命令失败/超时（exec 会透传远端\n"
                "exit_code）；5 网络/服务端错误。脚本化时用退出码判成败，别解析文案。\n",
                version=version,
                console_host=console_host,
            ),
        )
    filename = f"observatory-agent-skill-{version}.zip"
    log.info(
        "agent-skill.download ip=%s version=%s", client_ip(request), version
    )
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


__all__ = ["router"]
