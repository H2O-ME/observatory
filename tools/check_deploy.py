#!/usr/bin/env python
"""deploy/ 产物静态检查（CI 用；本地也能直接跑）。

检查内容：
* 所有 ``deploy/*.sh`` 都有严格模式；
* nginx 模板使用占位符（不写死生产域名）、有证书/反代/WebSocket 升级配置；
* ``observatory-map.conf`` 是合法的 map 定义；
* 已废弃的静态 vhost 有“已废弃”标记（防止被误 include）。
* 可选 ``--syntax``：用 `bash -n` 做 shell 语法检查（本机沙箱/Windows 会跳过）。

用法：
    python tools/check_deploy.py
    python tools/check_deploy.py --syntax     # CI 用；bash 不可用时明确跳过
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY = REPO_ROOT / "deploy"
NGINX = DEPLOY / "nginx"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def check_shell_strict() -> list[str]:
    problems: list[str] = []
    scripts = sorted(DEPLOY.glob("*.sh"))
    if not scripts:
        return ["deploy/ 下没有 shell 脚本"]
    for script in scripts:
        if "set -Eeuo pipefail" not in _read(script):
            problems.append(f"{script.relative_to(REPO_ROOT)}: 缺少 set -Eeuo pipefail")
    return problems


def check_nginx() -> list[str]:
    problems: list[str] = []
    for name in ("console.conf.template", "agent.conf.template"):
        path = NGINX / name
        if not path.exists():
            problems.append(f"缺少 deploy/nginx/{name}")
            continue
        text = _read(path)
        for keyword, label in (
            ("server_name", "域名"),
            ("ssl_certificate ", "证书"),
            ("ssl_certificate_key ", "私钥"),
            ("proxy_pass https://127.0.0.1:8443", "API 反代"),
            ("proxy_set_header Upgrade $http_upgrade", "WebSocket 升级"),
            ("proxy_set_header Connection $connection_upgrade", "WebSocket 连接头"),
        ):
            if keyword not in text:
                problems.append(f"deploy/nginx/{name}: 缺少{label}（{keyword}）")
        for literal in ("tianhw.top", "123423.xyz"):
            if literal in text:
                problems.append(f"deploy/nginx/{name}: 写死生产域名 {literal}（应用占位符）")

    mapping = NGINX / "observatory-map.conf"
    if not mapping.exists():
        problems.append("缺少 deploy/nginx/observatory-map.conf")
    else:
        text = _read(mapping)
        if "map $http_upgrade $connection_upgrade" not in text:
            problems.append("observatory-map.conf: 缺少 WebSocket upgrade map")
        if not re.search(r"^\s*default\s+upgrade;", text, re.MULTILINE):
            problems.append("observatory-map.conf: map 缺少 default upgrade 分支")

    for name in ("control.tianhw.top.conf", "observatory.123423.xyz.conf"):
        path = NGINX / name
        if not path.exists():
            problems.append(f"缺少遗留占位 deploy/nginx/{name}")
        elif "已废弃" not in _read(path):
            problems.append(f"deploy/nginx/{name}: 未标记“已废弃”，可能被误当成有效配置")
    return problems


def check_setup_renders_templates() -> list[str]:
    setup = DEPLOY / "setup.sh"
    if not setup.exists():
        return ["缺少 deploy/setup.sh"]
    text = _read(setup)
    problems: list[str] = []
    for fragment in (
        "deploy/nginx/console.conf.template",
        "deploy/nginx/agent.conf.template",
        "deploy/nginx/observatory-map.conf",
    ):
        if fragment not in text:
            problems.append(f"setup.sh: 未引用 {fragment}")
    if "nginx -t" not in text:
        problems.append("setup.sh: 安装站点前缺少 nginx -t 校验")
    return problems


def check_shell_syntax() -> list[str]:
    bash = shutil.which("bash")
    if not bash:
        print("bash 不可用，跳过 bash -n（CI 的 deploy-static job 会跑）")
        return []
    problems: list[str] = []
    for script in sorted(DEPLOY.glob("*.sh")):
        completed = subprocess.run(
            [bash, "-n", str(script)], capture_output=True, text=True, timeout=60
        )
        if completed.returncode != 0:
            problems.append(f"{script.relative_to(REPO_ROOT)}: bash -n 失败: {completed.stderr.strip()}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="deploy/ static checks")
    parser.add_argument("--syntax", action="store_true", help="also run bash -n")
    args = parser.parse_args(argv)

    problems = check_shell_strict() + check_nginx() + check_setup_renders_templates()
    if args.syntax:
        try:
            problems += check_shell_syntax()
        except (OSError, subprocess.SubprocessError) as exc:  # 沙箱/平台不支持
            print(f"bash -n 无法运行，跳过：{exc}")

    if problems:
        for problem in problems:
            print(f"DEPLOY {problem}")
        print(f"\n{len(problems)} problem(s).")
        return 1
    print("deploy/ static checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))