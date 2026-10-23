"""Command line entry point: ``python -m observatory <command>``.

Subcommands are unchanged from 0.2.x (``serve/version/enroll-code/packages/
package-add``). The CLI reads/writes only core modules; it never imports the
legacy compatibility layer.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
from pathlib import Path

import sqlalchemy

from .core.config import Settings, get_settings, resolve_version
from .core.crypto import new_device_token
from .core.db import Database
from .core.models import Package

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .main import create_app

    settings: Settings = get_settings()
    app = create_app(settings)
    kwargs: dict = {
        "host": args.host,
        "port": args.port,
        "log_level": args.log_level,
        "proxy_headers": settings.trust_forwarded_for,
        "forwarded_allow_ips": "127.0.0.1" if settings.trust_forwarded_for else "",
        "server_header": False,
    }
    if args.ssl_cert and args.ssl_key:
        kwargs["ssl_certfile"] = args.ssl_cert
        kwargs["ssl_keyfile"] = args.ssl_key
    uvicorn.run(app, **kwargs)
    return 0


def _version(_args: argparse.Namespace) -> int:
    settings = get_settings()
    print(resolve_version())
    print(f"  data_dir     : {settings.data_dir}")
    print(f"  console_host : {settings.console_hosts}")
    print(f"  agent_hosts  : {settings.agent_host_list}")
    print(f"  base_urls    : {settings.base_urls}")
    print("  bind default : 127.0.0.1:8443")
    return 0


def _enroll_code(_args: argparse.Namespace) -> int:
    # Device tokens and enrollment codes share one entropy source/format.
    code = new_device_token()
    print("把下面这行写入环境（.env）后重启服务：")
    print(f"OBS_ENROLL_CODE={code}")
    return 0


def _package_version(filename: str) -> str:
    match = _VERSION_RE.search(filename)
    if not match:
        raise ValueError(f"文件名里没有版本号: {filename}")
    return match.group(0)


def _package_list(_args: argparse.Namespace) -> int:
    async def run() -> int:
        settings = get_settings()
        database = Database(settings)
        await database.migrate()
        try:
            async with database.sessionmaker() as session:
                rows = (
                    await session.execute(sqlalchemy.select(Package).order_by(Package.version))
                ).scalars().all()
                if not rows:
                    print("没有已上传的受控端安装包。")
                    return 0
                print(f"{'版本':<10}{'大小':>10}  sha256")
                for row in rows:
                    print(f"{row.version:<10}{row.size_bytes:>10}  {(row.sha256 or '')[:16]}…")
        finally:
            await database.close()
        return 0

    return asyncio.run(run())


def _package_add(args: argparse.Namespace) -> int:
    from .core.clock import now_ms

    path = Path(args.path)
    if not path.is_file():
        print(f"找不到文件: {path}", file=sys.stderr)
        return 1
    data = path.read_bytes()
    if not data.startswith(b"PK"):
        print("不是有效的 zip 文件", file=sys.stderr)
        return 1
    try:
        version = _package_version(path.name)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    settings = get_settings()
    target = settings.packages_dir / f"observatory-agent-{version}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

    async def run() -> int:
        database = Database(settings)
        await database.migrate()
        try:
            async with database.sessionmaker() as session:
                existing = (
                    await session.execute(
                        sqlalchemy.select(Package).where(Package.kind == "agent", Package.version == version)
                    )
                ).scalars().first()
                if existing is None:
                    existing = Package(kind="agent", version=version, filename=target.name)
                    session.add(existing)
                existing.filename = target.name
                existing.size_bytes = len(data)
                existing.sha256 = hashlib.sha256(data).hexdigest()
                existing.notes = args.notes[:255]
                existing.created_at_ms = now_ms()
                await session.commit()
                print(f"已登记 {version}: {target.name} ({len(data)} bytes)")
        finally:
            await database.close()
        return 0

    return asyncio.run(run())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="observatory", description="Observatory 控制服务器")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动 HTTP(S) 服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8443)
    serve.add_argument("--ssl-cert", default="")
    serve.add_argument("--ssl-key", default="")
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=_serve)

    version = sub.add_parser("version", help="显示版本与解析后的配置")
    version.set_defaults(func=_version)

    code = sub.add_parser("enroll-code", help="生成一个注册码")
    code.set_defaults(func=_enroll_code)

    packages = sub.add_parser("packages", help="受控端安装包")
    packages.set_defaults(func=_package_list)

    add = sub.add_parser("package-add", help="登记一个受控端 zip")
    add.add_argument("path")
    add.add_argument("--notes", default="")
    add.set_defaults(func=_package_add)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))