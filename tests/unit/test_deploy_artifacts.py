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



def test_ghcr_pull_and_restart_fails_closed_to_old_image() -> None:
    """上线脚本（pull-and-restart.sh）：先记回滚锚点和 DB 快照，健康不过就切回旧镜像。"""

    restart = _read("pull-and-restart.sh")

    # 旧镜像是秒级回滚锚点；换库前先做在线 SQLite 快照。
    assert "OLD_IMAGE=" in restart
    assert "a.backup(b)" in restart

    # 服务器只拉取，不构建。
    assert "compose pull" in restart
    assert "compose up -d --no-build" in restart

    # 健康验证要区分 crash（restarting/exited/dead）和慢启动（DB 迁移）。
    assert "restarting|exited|dead" in restart
    assert "/api/v1/health" in restart

    # 失败必须回到旧镜像并退出非 0，不允许留下 crash loop。
    assert "rolling back to" in restart
    assert 'OBS_IMAGE_TAG="$OLD_IMAGE" compose up -d --no-build' in restart
    assert "exit 1" in restart


def test_setup_bootstraps_non_root_ownership() -> None:
    """521 回归：非 root（10001）镜像启动前必须校正数据卷与证书属主。"""

    setup = _read("setup.sh")
    assert "ensure_volume_owner" in setup
    assert 'chown -R "$OBS_UID:$OBS_UID" /data' in setup
    assert 'chown "$OBS_UID:$OBS_UID" "$APP_DIR/certs/server.crt"' in setup



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

