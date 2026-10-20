"""Command line entry point for the control server."""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys

from .config import get_settings, reset_settings_cache
from .version import resolve_version


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .main import create_app

    app = create_app()
    kwargs: dict = {
        "host": args.host,
        "port": args.port,
        "log_level": args.log_level,
        "proxy_headers": True,
        "forwarded_allow_ips": "*",
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
    return 0


def _enroll_code(_args: argparse.Namespace) -> int:
    code = secrets.token_urlsafe(18)
    print("把下面这行写入 .env，然后重启服务：")
    print(f"OBS_ENROLL_CODE={code}")
    return 0


def _package_list(_args: argparse.Namespace) -> int:
    async def run() -> int:
        from .services.runtime import Runtime

        settings = get_settings()
        reset_settings_cache()
        runtime = await Runtime.create(settings)
        try:
            async with runtime.db.sessionmaker() as session:
                packages = await runtime.packages.list(session, "agent")
                if not packages:
                    print("没有已上传的受控端安装包。")
                    return 0
                print(f"{'版本':<10}{'大小':>10}  sha256")
                for package in packages:
                    print(f"{package.version:<10}{package.size_bytes:>10}  {package.sha256[:16]}…")
        finally:
            await runtime.shutdown()
        return 0

    return asyncio.run(run())


def _package_add(args: argparse.Namespace) -> int:
    async def run() -> int:
        from pathlib import Path

        from .services.runtime import Runtime

        path = Path(args.path)
        if not path.is_file():
            print(f"找不到文件: {path}", file=sys.stderr)
            return 1
        runtime = await Runtime.create(get_settings())
        try:
            async with runtime.db.sessionmaker() as session:
                package = await runtime.packages.save(
                    session, filename=path.name, data=path.read_bytes(), notes=args.notes
                )
                print(f"已登记 {package.version}: {package.filename} ({package.size_bytes} bytes)")
        finally:
            await runtime.shutdown()
        return 0

    return asyncio.run(run())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="observatory", description="Observatory 控制服务器")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动 HTTP(S) 服务")
    serve.add_argument("--host", default="0.0.0.0")
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))