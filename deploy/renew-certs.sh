#!/usr/bin/env bash
# 证书续期。由 cron 每天运行一次，或在 deploy/setup.sh 之后手动装一个 systemd timer。
#
#   sudo bash deploy/renew-certs.sh
#
# container 在启动时读取证书，所以续期成功后要重启容器。

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/observatory}"

log() { printf '\033[36m[certs]\033[0m %s\n' "$*"; }

if ! certbot renew --quiet --deploy-hook "systemctl reload nginx"; then
  log "certbot renew 没有可续期的证书或执行失败"
fi

# 只有证书真的更新了才重启容器（比较 mtime）。
latest="$(find /etc/letsencrypt/live -name fullchain.pem -mmin -90 | head -n 1 || true)"
if [[ -n "$latest" ]]; then
  log "证书已更新，重启容器以加载新证书"
  cd "$APP_DIR"
  docker compose --env-file server/.env restart api
else
  log "证书未更新，无需重启"
fi