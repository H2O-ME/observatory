#!/usr/bin/env bash
# Observatory 0.3.0 —— 首次部署 / 一键安装。
#
#   sudo REPO_URL=git@github.com:<you>/observatory.git \
#        CERTBOT_EMAIL=you@example.com \
#        bash deploy/setup.sh
#
# 顺序（0.3.0 修订）：
#   1. 校验域名 / root / apt 发行版
#   2. 安装 docker + compose（Debian 与 Ubuntu 使用各自官方源）
#   3. 安装 nginx + certbot（两者分别检测，不再用 nginx 的存在性代替 certbot）
#   4. 准备代码与 server/.env（含 compose 层的绑定地址/资源限制变量）
#   5. 用 ACME bootstrap vhost 占住 80 → certbot --webroot 申请单张双域名证书
#   6. 把证书**实体拷贝**到 /opt/observatory/certs（非符号链接，容器可见）
#   7. 渲染 nginx 站点模板 → nginx -t → reload
#   8. 构建并启动容器；健康检查失败必须失败退出（不再打印“部署完成”）
#
# 脚本可以重复运行：已有 .env、证书、数据卷都不会被覆盖。
# 续期见 deploy/renew-certs.sh（webroot，失败退出非 0）。

set -Eeuo pipefail
IFS=$'\n\t'

APP_DIR="${APP_DIR:-/opt/observatory}"
REPO_URL="${REPO_URL:-}"
CONSOLE_HOST="${CONSOLE_HOST:-control.tianhw.top}"
AGENT_HOST="${AGENT_HOST:-observatory.123423.xyz}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"
# 单张证书同时覆盖两个域名；续期、nginx 与容器都引用同一个 lineage。
CERT_NAME="${CERT_NAME:-observatory}"
ACME_WEBROOT="${ACME_WEBROOT:-/var/www/certbot}"
# 容器内运行用户（与 server/Dockerfile 的 OBS_UID 默认值一致）。
OBS_UID="${OBS_UID:-10001}"
# 允许在衍生发行版上强制指定 Docker 源（debian/ubuntu）。
DOCKER_DISTRO="${DOCKER_DISTRO:-}"

STACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$STACK_DIR/.." && pwd)"

log()  { printf '\033[36m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[setup]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[setup] %s\033[0m\n' "$*" >&2; exit 1; }

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "请用 root 运行：sudo bash deploy/setup.sh"
  fi
}

validate_host() {
  local host="$1"
  [[ "$host" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || die "域名不合法：$1"
  [[ "$host" == *.* ]] || die "域名不合法（需要至少两级）：$1"
}

validate_inputs() {
  CONSOLE_HOST="$(printf '%s' "$CONSOLE_HOST" | tr '[:upper:]' '[:lower:]')"
  AGENT_HOST="$(printf '%s' "$AGENT_HOST" | tr '[:upper:]' '[:lower:]')"
  validate_host "$CONSOLE_HOST"
  validate_host "$AGENT_HOST"
  [[ "$CONSOLE_HOST" != "$AGENT_HOST" ]] || die "CONSOLE_HOST 与 AGENT_HOST 不能相同"
  case "$CERT_NAME" in
    *[!a-z0-9-]*) die "CERT_NAME 只允许小写字母、数字与连字符：$CERT_NAME" ;;
  esac
}

install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log "docker 已就绪：$(docker --version)"
    return
  fi
  command -v apt-get >/dev/null 2>&1 || die "0.3.0 部署脚本只支持 Debian/Ubuntu（需要 apt-get）"
  log "安装 docker 与 compose 插件"
  apt-get update -y
  apt-get install -y --no-install-recommends ca-certificates curl gnupg openssl
  install -m 0755 -d /etc/apt/keyrings

  # A24：Ubuntu 用 linux/ubuntu，Debian 用 linux/debian。
  # 旧脚本统一写死 /linux/debian，在 Ubuntu 上会因 codename 不存在而失败。
  local os_id="" codename="" docker_distro=""
  # shellcheck disable=SC1091
  . /etc/os-release
  os_id="${ID:-}"
  codename="${VERSION_CODENAME:-}"
  docker_distro="${DOCKER_DISTRO:-}"
  if [[ -z "$docker_distro" ]]; then
    case "$os_id" in
      debian) docker_distro="debian" ;;
      ubuntu) docker_distro="ubuntu" ;;
      *)
        if grep -qi '^ID_LIKE=.*ubuntu' /etc/os-release; then
          docker_distro="ubuntu"
          codename="${UBUNTU_CODENAME:-$codename}"
        elif grep -qi '^ID_LIKE=.*debian' /etc/os-release; then
          docker_distro="debian"
        else
          die "不支持的发行版：${os_id:-未知}（仅支持 Debian/Ubuntu；可用 DOCKER_DISTRO=debian|ubuntu 覆盖）"
        fi
        ;;
    esac
  fi
  [[ -n "$codename" ]] || die "无法确定发行版 codename（/etc/os-release 缺 VERSION_CODENAME）"

  curl -fsSL "https://download.docker.com/linux/${docker_distro}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${docker_distro} ${codename} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
}

install_nginx_certbot() {
  # A6：nginx 与 certbot 分别检测；旧脚本只看 nginx，机器上装了 nginx 却缺
  # certbot 时会跳过安装，随后 certbot 命令不存在。
  local packages=()
  command -v nginx  >/dev/null 2>&1 || packages+=(nginx)
  command -v certbot >/dev/null 2>&1 || packages+=(certbot python3-certbot-nginx)
  command -v openssl >/dev/null 2>&1 || packages+=(openssl)
  if [[ ${#packages[@]} -gt 0 ]]; then
    log "安装 nginx / certbot / openssl：${packages[*]}"
    apt-get update -y
    apt-get install -y --no-install-recommends "${packages[@]}"
  fi
  mkdir -p "$ACME_WEBROOT"
  chmod 0755 "$ACME_WEBROOT"
  systemctl enable --now nginx
}

ensure_repo() {
  if [[ -d "$APP_DIR/.git" ]]; then
    log "已有仓库，跳过克隆：$APP_DIR"
    return
  fi
  if [[ -z "$REPO_URL" ]]; then
    if [[ -d "$REPO_ROOT/.git" ]]; then
      log "使用当前目录作为部署源：$REPO_ROOT"
      if [[ "$REPO_ROOT" != "$APP_DIR" ]]; then
        mkdir -p "$(dirname "$APP_DIR")"
        cp -a "$REPO_ROOT" "$APP_DIR"
      fi
      return
    fi
    die "请设置 REPO_URL=git@github.com:you/observatory.git 后重试"
  fi
  log "克隆仓库到 $APP_DIR"
  mkdir -p "$(dirname "$APP_DIR")"
  git clone "$REPO_URL" "$APP_DIR"
}

ensure_env() {
  local env_file="$APP_DIR/server/.env"
  if [[ -f "$env_file" ]]; then
    log "server/.env 已存在，保留现有配置"
    return
  fi
  log "生成 server/.env（随机密钥）"
  mkdir -p "$APP_DIR/server"
  local secret admin enroll
  secret="$(head -c 48 /dev/urandom | base64 | tr -d '\n=+/' | cut -c1-64)"
  admin="$(head -c 12 /dev/urandom | base64 | tr -d '\n=+/' | cut -c1-16)"
  enroll="$(head -c 18 /dev/urandom | base64 | tr -d '\n=+/' | cut -c1-24)"
  cat > "$env_file" <<EOF
# 由 deploy/setup.sh 生成（Observatory 0.3.0）。请勿提交到仓库。
OBS_SECRET_KEY=$secret
OBS_ADMIN_PASSWORD=$admin
# 0.2.x 受控端兼容用的共享注册码；0.3.0 新安装走一次性安装令牌。
OBS_ENROLL_CODE=$enroll
# 一次性安装令牌 TTL 是控制台设置（install_token_ttl_minutes），不是 env。
OBS_DATA_DIR=/data
OBS_CONSOLE_HOST=$CONSOLE_HOST
OBS_AGENT_HOSTS=$AGENT_HOST,$CONSOLE_HOST
OBS_PUBLIC_BASE_URLS=https://$CONSOLE_HOST,https://$AGENT_HOST
# 8443 只绑回环，由本机 nginx 反代；只有在这种拓扑下才信任转发头。
OBS_BIND_ADDR=127.0.0.1
OBS_PORT=8443
OBS_TRUST_FORWARDED_FOR=true
# 容器运行用户 / 资源限制（compose 读取）。
OBS_UID=$OBS_UID
OBS_MEM_LIMIT=1g
OBS_CPUS=2.0
EOF
  chmod 600 "$env_file"
  warn "已生成随机管理员密码与注册码，记录在这里： $env_file"
  grep -E 'OBS_ADMIN_PASSWORD|OBS_ENROLL_CODE' "$env_file" || true
}

ensure_env_defaults() {
  # 旧版 .env 没有 compose 层变量：补默认值，但不覆盖已有值。
  local env_file="$APP_DIR/server/.env"
  local pair key value
  for pair in "OBS_BIND_ADDR=127.0.0.1" "OBS_PORT=8443" "OBS_TRUST_FORWARDED_FOR=true"; do
    key="${pair%%=*}"
    value="${pair#*=}"
    if ! grep -q "^${key}=" "$env_file"; then
      printf '%s=%s\n' "$key" "$value" >> "$env_file"
      warn "server/.env 缺少 ${key}，已追加默认值 ${value}"
    fi
  done
}

install_acme_bootstrap() {
  # 首次申请证书前，80 端口必须由 nginx 提供 ACME 路径（webroot 模式），
  # 且 nginx 不能停——否则会重蹈旧方案（先停 nginx 再独立申请）续期失败的覆辙。
  local conf=/etc/nginx/sites-available/observatory-acme.conf
  log "安装 ACME bootstrap vhost（80 端口，webroot=$ACME_WEBROOT）"
  cat > "$conf" <<EOF
# 由 deploy/setup.sh 生成：仅用于证书申请，安装完正式站点后会被移除。
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name $CONSOLE_HOST $AGENT_HOST;

    location /.well-known/acme-challenge/ {
        root $ACME_WEBROOT;
    }

    location / {
        return 404;
    }
}
EOF
  chmod 0644 "$conf"
  ln -sfn "$conf" /etc/nginx/sites-enabled/observatory-acme.conf
  rm -f /etc/nginx/sites-enabled/default
  nginx -t || die "nginx 配置校验失败（ACME bootstrap）；请检查是否已有其它 default_server"
  systemctl reload-or-restart nginx
}

ensure_certs() {
  local cert_dir="/etc/letsencrypt/live/$CERT_NAME"
  [[ -n "$CERTBOT_EMAIL" ]] || die "首次部署需要 CERTBOT_EMAIL=you@example.com 用于申请证书"

  if [[ -f "$cert_dir/fullchain.pem" && -f "$cert_dir/privkey.pem" ]]; then
    log "证书已存在，跳过申请：$cert_dir"
  else
    log "申请单张双域名证书：$CONSOLE_HOST, $AGENT_HOST（webroot）"
    certbot certonly --webroot -w "$ACME_WEBROOT" \
      --cert-name "$CERT_NAME" \
      --non-interactive --agree-tos -m "$CERTBOT_EMAIL" \
      --keep-until-expiring \
      -d "$CONSOLE_HOST" -d "$AGENT_HOST" \
      || die "证书申请失败：检查 80 端口、ACME 路径 $ACME_WEBROOT 与两个域名的解析"
  fi

  [[ -f "$cert_dir/fullchain.pem" && -f "$cert_dir/privkey.pem" ]] \
    || die "证书文件不完整：$cert_dir"

  # A6：双域名断言 + 有效期断言，任何一项不满足都不继续。
  local host
  for host in "$CONSOLE_HOST" "$AGENT_HOST"; do
    openssl x509 -in "$cert_dir/fullchain.pem" -noout -checkhost "$host" 2>/dev/null \
      | grep -q 'does match' \
      || die "证书不包含域名 $host（SAN 不匹配）"
  done
  openssl x509 -in "$cert_dir/fullchain.pem" -noout -checkend 86400 >/dev/null 2>&1 \
    || die "证书将在 24 小时内过期，拒绝继续部署"

  # A6：实体拷贝（install）而不是绝对符号链接；容器只挂 ../certs，
  # 绝对符号链接在容器内会指向容器自己的 /etc/letsencrypt（不存在）。
  install -d -m 0755 "$APP_DIR/certs"
  install -m 0644 "$cert_dir/fullchain.pem" "$APP_DIR/certs/server.crt"
  install -m 0600 "$cert_dir/privkey.pem" "$APP_DIR/certs/server.key"
  # 非 root 容器用户（OBS_UID）需要能读私钥：属主改为容器 UID + 600。
  chown "$OBS_UID:$OBS_UID" "$APP_DIR/certs/server.crt" "$APP_DIR/certs/server.key"
  log "已复制实体证书到 $APP_DIR/certs（属主 $OBS_UID，非符号链接）"
}

render_site() {
  local template="$1" target="$2"
  sed -e "s|@@CONSOLE_HOST@@|$CONSOLE_HOST|g" \
      -e "s|@@AGENT_HOST@@|$AGENT_HOST|g" \
      -e "s|@@CERT_NAME@@|$CERT_NAME|g" \
      "$template" > "$target"
  chmod 0644 "$target"
}

reload_nginx() {
  # Debian/Ubuntu 用 systemd；宝塔等面板可能只通过 init.d 启动（systemd 显示
  # inactive）。两种都尝试，全部失败才返回非 0，避免“证书换了但 nginx 没加载”。
  if systemctl is-active --quiet nginx 2>/dev/null; then
    systemctl reload nginx
    return $?
  fi
  nginx -s reload
}

install_nginx_sites() {
  log "渲染并安装 nginx 站点（模板 → sites-available）"
  # WebSocket map 只能定义一次：放到 conf.d，两个 vhost 共用。
  install -m 0644 "$APP_DIR/deploy/nginx/observatory-map.conf" /etc/nginx/conf.d/observatory-map.conf
  render_site \
    "$APP_DIR/deploy/nginx/console.conf.template" \
    /etc/nginx/sites-available/observatory-console.conf
  render_site \
    "$APP_DIR/deploy/nginx/agent.conf.template" \
    /etc/nginx/sites-available/observatory-agent.conf
  ln -sfn /etc/nginx/sites-available/observatory-console.conf /etc/nginx/sites-enabled/observatory-console.conf
  ln -sfn /etc/nginx/sites-available/observatory-agent.conf /etc/nginx/sites-enabled/observatory-agent.conf
  rm -f /etc/nginx/sites-enabled/observatory-acme.conf
  rm -f /etc/nginx/sites-enabled/default
  # 迁移：移除旧版以域名命名的站点软链（文件本身留在 sites-available）。
  rm -f "/etc/nginx/sites-enabled/$CONSOLE_HOST.conf" "/etc/nginx/sites-enabled/$AGENT_HOST.conf"
  nginx -t || die "nginx 配置校验失败（证书路径或站点模板）；未 reload"
  reload_nginx || die "nginx reload 失败（systemd 与 nginx -s reload 都不可用）"
  log "nginx 已 reload"
}

wait_health() {
  local tries="${1:-60}" i
  for ((i = 1; i <= tries; i++)); do
    if curl -fsSk "https://127.0.0.1:8443/api/v1/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

ensure_volume_owner() {
  # 0.2.x 的数据卷属主是 root；0.3.0 容器以非 root 运行，首次升级必须校正。
  if docker volume inspect observatory-data >/dev/null 2>&1; then
    log "校正数据卷属主为容器用户 $OBS_UID"
    docker run --rm -v observatory-data:/data alpine:3 chown -R "$OBS_UID:$OBS_UID" /data \
      || warn "数据卷 chown 失败；容器可能无法写 /data（详见 docs/部署.md）"
  fi
}

start_stack() {
  local version served
  version="$(tr -d '[:space:]' < "$APP_DIR/VERSION")"
  log "构建并启动容器（镜像 tag: $version）"
  cd "$APP_DIR"
  ensure_volume_owner
  export OBS_IMAGE_TAG="$version"
  docker compose -f deploy/docker-compose.yml --env-file server/.env up -d --build \
    || die "容器构建/启动失败；查看 docker compose logs"
  if ! wait_health 60; then
    docker compose -f deploy/docker-compose.yml --env-file server/.env logs --tail=80 || true
    die "健康检查 120 秒未通过（证书/数据库/端口），未视为部署成功"
  fi
  served=$(curl -fsSk https://127.0.0.1:8443/api/v1/version \
    | sed -n 's/.*"version":"\([^"]*\)".*/\1/p' || true)
  [[ "$served" == "$version" ]] || die "运行版本 $served 与仓库 VERSION $version 不一致"
  docker tag "observatory/api:$version" "observatory/api:latest" >/dev/null 2>&1 \
    || warn "无法追加 latest tag（非致命）"
  log "服务已就绪，版本 $version"
}

summary() {
  local version
  version="$(tr -d '[:space:]' < "$APP_DIR/VERSION")"
  cat <<EOF

部署完成。

控制台    : https://$CONSOLE_HOST
受控端接入: https://$CONSOLE_HOST  与  https://$AGENT_HOST （互为备份）
证书      : $APP_DIR/certs/server.crt（实体拷贝，0.3.0 起不再用符号链接）
安装命令  : 控制台 → 设置 → 安装程序（一次性安装令牌，默认 24 小时、单次使用）
版本      : $version

下一步：
  1. 上传受控端安装包（控制台 → 更新 → 上传），或 sudo bash deploy/build-agent.sh
  2. 在目标机器运行安装程序（只会弹一次 UAC）
  3. 证书续期（每天一次，失败退出非 0，可用于监控告警）：
      0 4 * * * root bash $APP_DIR/deploy/renew-certs.sh >/dev/null 2>&1
  4. 上线体检：python tools/check_deployment.py --password '<密码>' --code '<注册码或安装令牌>'
EOF
}

main() {
  require_root
  validate_inputs
  install_docker
  install_nginx_certbot
  ensure_repo
  ensure_env
  ensure_env_defaults
  install_acme_bootstrap
  ensure_certs
  install_nginx_sites
  start_stack
  summary
}

main "$@"