#!/usr/bin/env bash
# 证书续期（Observatory 0.3.0）。
#
#   sudo bash deploy/renew-certs.sh
#
# 由 cron 每天运行一次：
#   0 4 * * * root bash /opt/observatory/deploy/renew-certs.sh >/dev/null 2>&1
#
# A6 契约：
#   * 与首次申请使用同一种方式：certbot --webroot（nginx 常驻 80，不停止）；
#   * 续期失败必须退出非 0（旧版把失败吞掉，cron 永远看不到告警）；
#   * 续期成功后重新**实体拷贝**证书到 /opt/observatory/certs，并重启容器；
#   * 证书剩余不足 14 天时打印告警（便于监控 grep）。

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/observatory}"
CERT_NAME="${CERT_NAME:-observatory}"
ACME_WEBROOT="${ACME_WEBROOT:-/var/www/certbot}"
OBS_UID="${OBS_UID:-10001}"
CERT_DIR="/etc/letsencrypt/live/$CERT_NAME"

log()  { printf '\033[36m[certs]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[certs]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[certs] %s\033[0m\n' "$*" >&2; exit 1; }

if [[ "${EUID}" -ne 0 ]]; then
  die "请用 root 运行：sudo bash deploy/renew-certs.sh"
fi

[[ -f "$CERT_DIR/fullchain.pem" ]] || die "找不到证书 $CERT_DIR/fullchain.pem；先运行 deploy/setup.sh"
[[ -d "$ACME_WEBROOT" ]] || die "ACME webroot 不存在：$ACME_WEBROOT"

fingerprint() {
  sha256sum "$CERT_DIR/fullchain.pem" 2>/dev/null | cut -d' ' -f1 || true
}

copy_certs() {
  install -d -m 0755 "$APP_DIR/certs"
  install -m 0644 "$CERT_DIR/fullchain.pem" "$APP_DIR/certs/server.crt"
  install -m 0600 "$CERT_DIR/privkey.pem" "$APP_DIR/certs/server.key"
  # 与 setup.sh 一致：非 root 容器用户需要读取私钥。
  chown "$OBS_UID:$OBS_UID" "$APP_DIR/certs/server.crt" "$APP_DIR/certs/server.key"
}

BEFORE="$(fingerprint)"
log "certbot renew（webroot: $ACME_WEBROOT）"
if ! certbot renew --cert-name "$CERT_NAME" --webroot -w "$ACME_WEBROOT" --quiet \
     --deploy-hook "systemctl reload nginx 2>/dev/null || nginx -s reload"; then
  die "certbot renew 失败（证书未续期；cron 会收到非 0 退出码）"
fi
AFTER="$(fingerprint)"

if [[ "$BEFORE" != "$AFTER" ]]; then
  log "证书已更新，重新拷贝实体证书并重启容器"
  copy_certs
  cd "$APP_DIR"
  docker compose -f deploy/docker-compose.yml --env-file server/.env restart api \
    || die "容器重启失败，请检查 docker compose logs"
  log "容器已重启加载新证书"
else
  log "证书未变化（未到续期时间）"
  # 若 certs/ 下的实体副本丢失（例如误删），仍补齐，避免下次重启 TLS 失败。
  if [[ ! -f "$APP_DIR/certs/server.crt" || ! -f "$APP_DIR/certs/server.key" ]]; then
    warn "certs/ 实体副本缺失，已从 $CERT_DIR 补齐"
    copy_certs
  fi
fi

# 可观测性：剩余天数不足 14 时告警。
not_after="$(openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -enddate 2>/dev/null | cut -d= -f2- || true)"
if [[ -n "$not_after" ]]; then
  end_epoch="$(date -d "$not_after" +%s 2>/dev/null || true)"
  now_epoch="$(date +%s)"
  if [[ -n "$end_epoch" ]]; then
    remaining_days=$(( (end_epoch - now_epoch) / 86400 ))
    log "证书剩余有效期：${remaining_days} 天"
    if [[ "$remaining_days" -lt 14 ]]; then
      warn "证书将在 ${remaining_days} 天内过期，请检查 CERTBOT/80 端口/域名解析"
    fi
  fi
fi

exit 0