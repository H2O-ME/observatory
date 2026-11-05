"""共享测试入口：把历史自检命令路由到新 tests/（spec §9）。

背景：0.2.10 的 `tools/test_server.py`、`tools/test_agent.py`、`tools/test_e2e_live.py`
是手写脚本，逻辑已迁移为 pytest 用例。对外命令名保留（CI/文档/运维习惯），
内部统一走 pytest，避免两套测试体系各自漂移。

用法（脚本内部调用，不直接给用户）：
    run(LAYER_SERVER, extra_args)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

LAYER_SERVER = "server"
LAYER_AGENT = "agent"
LAYER_E2E = "e2e"

_TARGETS = {
    LAYER_SERVER: ["tests/unit", "tests/contract", "tests/acceptance", "tests/integration"],
    LAYER_AGENT: ["tests/unit", "tests/contract", "tests/integration"],
    LAYER_E2E: ["tests/integration"],
}

_BANNERS = {
    LAYER_SERVER: "服务端自检（tests/: unit + contract + acceptance + integration）",
    LAYER_AGENT: "受控端逻辑自检（tests/: unit + contract + integration）",
    LAYER_E2E: "真机端到端（tests/integration，W1 前为 skip）",
}


def run(layer: str, extra: list[str]) -> int:
    targets = _TARGETS[layer]
    command = [sys.executable, "-m", "pytest", *targets, "-q", *extra]
    print("=" * 72)
    print(_BANNERS[layer])
    print("旧 0.2.10 手写自检已迁移到 tests/；原实现见 tag pre-rewrite-0.2.10。")
    print("执行：", " ".join(command))
    print("=" * 72, flush=True)
    try:
        return subprocess.call(command, cwd=str(REPO_ROOT))
    except FileNotFoundError:
        print("未找到 pytest：请先 pip install -r requirements-dev.txt", file=sys.stderr)
        return 2


__all__ = ["LAYER_AGENT", "LAYER_E2E", "LAYER_SERVER", "run"]