#!/usr/bin/env python
"""受控端逻辑自检（对外入口保留，内部调用 tests/，spec §9）。

    python tools/test_agent.py
    python tools/test_agent.py -k envelope       # 额外参数透传给 pytest

0.2.10 的纯逻辑自检（版本/配置/帧协议/传输阶梯/更新/卸载范围）已迁移为：
- `tests/unit/`（纯函数）
- `tests/contract/`（协议与 legacy 兼容）
- `tests/integration/`（W1 后接真受控端）
Windows 系统层用例带 `windows_only` 标记，在 Linux 上由 skip 处理。
原实现保留在 tag `pre-rewrite-0.2.10`。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pytest_entry import LAYER_AGENT, run  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run(LAYER_AGENT, sys.argv[1:]))