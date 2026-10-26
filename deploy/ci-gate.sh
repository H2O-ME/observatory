#!/usr/bin/env bash
# CI 部署密钥的 SSH forced-command 网关。
#
# authorized_keys 里以
#   command="/opt/observatory/deploy/ci-gate.sh",no-port-forwarding,no-pty,... ssh-ed25519 AAAA... observatory-ci
# 安装的密钥只能执行本文件放行的命令，其余一律拒绝。
# 放行集合（与 deploy.sh 子命令一一对应）：
#   release vX.Y.Z   触发一次部署（异步）
#   status           查看状态文件
#   logs [N]         尾随部署日志

set -Eeuo pipefail

DEPLOY="/opt/observatory/deploy/deploy.sh"
cmd="${SSH_ORIGINAL_COMMAND:-}"

case "$cmd" in
  release\ *)
    tag="${cmd#release }"
    # 严格校验 tag 形态，杜绝把元字符带进 deploy.sh 的参数位。
    [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
      echo "ci-gate: 非法 tag：$tag" >&2
      exit 127
    }
    exec bash "$DEPLOY" release "$tag"
    ;;
  status)
    exec bash "$DEPLOY" status
    ;;
  "logs")
    exec bash "$DEPLOY" logs 80
    ;;
  logs\ *)
    n="${cmd#logs }"
    case "$n" in
      ''|*[!0-9]*) echo "ci-gate: logs 参数必须是数字" >&2; exit 127 ;;
    esac
    exec bash "$DEPLOY" logs "$n"
    ;;
  *)
    echo "ci-gate: 拒绝命令：${cmd:-<空>}（只允许 release vX.Y.Z / status / logs）" >&2
    exit 127
    ;;
esac
