#!/usr/bin/env bash
# 在部署服务器上安装并启用「GitHub 轮询 → 自动部署」timer。
#
#   sudo bash deploy/install-auto-update.sh
#   systemctl list-timers observatory-auto-update.timer
#   journalctl -u observatory-auto-update.service -n 80 --no-pager
#
# 安装后：本地 git push main → 2 分钟内服务器拉取并部署；
# 本地 push v<VERSION> tag → CI 构建受控端并推 agent-dist → 服务器自动登记。
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/observatory}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "请用 root 运行：sudo bash deploy/install-auto-update.sh" >&2
  exit 1
fi
[[ -x "$APP_DIR/deploy/auto-update.sh" ]] || {
  echo "$APP_DIR/deploy/auto-update.sh 不存在或不可执行" >&2
  exit 1
}
[[ -d "$APP_DIR/deploy/systemd" ]] || {
  echo "$APP_DIR/deploy/systemd 不存在" >&2
  exit 1
}

chmod 0755 "$APP_DIR/deploy/auto-update.sh"
install -m 0644 "$APP_DIR/deploy/systemd/observatory-auto-update.service" \
  /etc/systemd/system/observatory-auto-update.service
install -m 0644 "$APP_DIR/deploy/systemd/observatory-auto-update.timer" \
  /etc/systemd/system/observatory-auto-update.timer

systemctl daemon-reload
systemctl enable --now observatory-auto-update.timer
systemctl start observatory-auto-update.service || true
systemctl --no-pager --full status observatory-auto-update.timer || true
echo "已启用：observatory-auto-update.timer（每 2 分钟检查一次）"
