"""部署产物静态契约（A6/A7 的运维面；真正的 shell 语法检查在 CI 里跑 bash -n/shellcheck）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
NGINX = DEPLOY / "nginx"


def _read(relative: str) -> str:
    path = DEPLOY / relative
    assert path.exists(), f"缺少 deploy/{relative}"
    return path.read_text(encoding="utf-8")


def test_all_shell_scripts_use_strict_mode() -> None:
    scripts = sorted(DEPLOY.glob("*.sh"))
    assert scripts, "deploy/ 下应至少有一个运维脚本"
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert "set -Eeuo pipefail" in text, f"{script.name} 缺少严格模式"


def test_nginx_templates_are_dual_domain_and_websocket_ready() -> None:
    console = _read("nginx/console.conf.template")
    agent = _read("nginx/agent.conf.template")
    map_conf = _read("nginx/observatory-map.conf")

    assert "server_name @@CONSOLE_HOST@@" in console
    assert "server_name @@AGENT_HOST@@" in agent
    for text in (console, agent):
        assert "@@CERT_NAME@@" in text
        assert "ssl_certificate " in text and "ssl_certificate_key " in text
        assert "proxy_set_header Upgrade $http_upgrade" in text
        assert "proxy_set_header Connection $connection_upgrade" in text
        assert "proxy_pass https://127.0.0.1:8443" in text

    # map 只能定义一次，两个 vhost 共用
    assert "map $http_upgrade $connection_upgrade" in map_conf
    assert "default upgrade" in map_conf

    # agent 域名代理受控端与公开安装端点；控制台域名代理全部
    assert "location /api/v1/agent/" in agent
    assert "location /api/v1/public/" in agent
    assert "location / {" in console


def test_nginx_templates_have_no_literal_production_domains() -> None:
    """A23：模板只允许占位符，默认域名来自配置模块。"""

    for name in ("nginx/console.conf.template", "nginx/agent.conf.template"):
        text = _read(name)
        assert "tianhw.top" not in text, f"{name} 写死了生产域名"
        assert "123423.xyz" not in text, f"{name} 写死了生产域名"


def test_obsolete_static_confs_are_marked_deprecated() -> None:
    """旧静态站点配置保留为“已废弃”占位，避免被误 include。"""

    for name in ("nginx/control.tianhw.top.conf", "nginx/observatory.123423.xyz.conf"):
        text = _read(name)
        assert "已废弃" in text, name
        assert "console.conf.template" in text, name


def test_setup_renders_templates_instead_of_copying_static_confs() -> None:
    setup = _read("setup.sh")
    for fragment in (
        "nginx/console.conf.template",
        "nginx/agent.conf.template",
        "nginx/observatory-map.conf",
    ):
        assert fragment in setup, fragment
    assert "render_site" in setup
    # ACME bootstrap 也必须用实体 vhost + nginx -t 校验
    assert "nginx -t" in setup
    assert "observatory-acme.conf" in setup


def test_update_sh_is_deprecated_shim() -> None:
    """0.3.7 起 update.sh 只是人工兜底 shim：部署逻辑全部在 deploy.sh。"""

    update = _read("update.sh")
    assert "已废弃" in update
    assert "deploy/deploy.sh" in update
    assert "origin/main:VERSION" in update


def test_deploy_worker_preflights_non_root_ownership_and_fails_closed() -> None:
    """521 回归（迁到 CI 直推的 deploy.sh）：非 root 镜像启动前必须校正属主，失败即回滚，禁止并发。"""

    deploy = _read("deploy.sh")

    # 并发部署会把同一容器反复重建。
    assert "flock -n 9" in deploy
    assert "OBS_DEPLOY_LOCK_HELD" in deploy

    # 非 root 容器需要 /data 与 /certs 都属于容器 UID。
    assert "ensure_volume_owner" in deploy
    assert "ensure_cert_owner" in deploy
    assert 'chown "$uid:$uid" "$crt" "$key"' in deploy

    # chown 失败不能只是 warn 后继续：否则脚本报成功、容器 crash loop。
    assert "数据卷 chown 失败" in deploy
    assert '|| warn "数据卷 chown' not in deploy
    assert 'rollback "数据卷属主校正失败' in deploy
    assert 'rollback "证书属主/存在性校正失败' in deploy

    # 启动后先看容器是否在 restarting/exited，再等 health，缩短 521 窗口。
    assert "wait_started 30" in deploy
    assert "未进入 running" in deploy

    # 多点健康验证（2026-10-26 宕机教训）：真实 Host 头反代路径必须在列——
    # 只测回环会漏掉 HostSplit/反代层的策略回归。
    assert "in_container_health" in deploy
    assert "host_path_health" in deploy
    assert '-H "Host: $(console_host)"' in deploy
    assert "/data/webui/.version" in deploy


def test_release_script_runs_tests_locally_and_deploys_via_ssh() -> None:
    """CI 已删除（0.3.7 末）：发布脚本必须自带本地测试与 SSH 部署盯守。"""

    release = (REPO_ROOT / "tools" / "release.sh").read_text(encoding="utf-8")
    assert "pytest tests/unit tests/integration tests/contract tests/acceptance" in release
    assert "deploy.sh release $TAG" in release
    assert "result=failed" in release
    assert "actions/runs" not in release, "不应再依赖 GitHub Actions"


def test_nginx_templates_log_origin_traffic_for_521_diagnosis() -> None:
    """521 是 CDN→源站断链；源站没有 access/error log 就无法判断请求是否到达。"""

    for name in ("nginx/console.conf.template", "nginx/agent.conf.template"):
        text = _read(name)
        assert "access_log " in text, name
        assert "error_log " in text, name


def test_nginx_reload_works_without_systemd_panel() -> None:
    """宝塔面板用 init.d 启停 nginx（systemd 显示 inactive），必须回退 nginx -s reload。"""

    setup = _read("setup.sh")
    renew = _read("renew-certs.sh")
    assert "reload_nginx" in setup
    assert "nginx -s reload" in setup
    assert "nginx -s reload" in renew

