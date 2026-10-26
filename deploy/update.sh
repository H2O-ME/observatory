#!/usr/bin/env bash
# 已废弃（0.3.7 起）：部署链路改为 CI 直推——本机 tools/release.sh 打 tag →
# GitHub Actions 全门禁后 SSH 触发服务器 deploy/deploy.sh。
# 轮询自动更新（auto-update.timer）已拆除：它无法感知"发布意图"，会在开发途中
# 拉走 main 全量重建容器，且被 systemd 超时杀死时会留下半完成的回滚
# （2026-10-26 八小时宕机的直接原因）。
#
# 保留本入口作为人工兜底：部署 origin/main 当前 VERSION 对应的 tag。
set -Eeuo pipefail
IFS=$'\n\t'

APP_DIR="${APP_DIR:-/opt/observatory}"
echo "[update] 已废弃的入口；转为调用 deploy/deploy.sh release（CI 直推模式的人工兜底）" >&2

cd "$APP_DIR"
git fetch --quiet origin main
ver="$(git show origin/main:VERSION | tr -d '[:space:]')"
echo "[update] origin/main VERSION=$ver → 部署 v$ver"
exec bash "$APP_DIR/deploy/deploy.sh" release "v$ver" "$@"
