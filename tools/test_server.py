#!/usr/bin/env python
"""服务端自检（对外入口保留，内部调用 tests/，spec §9）。

    python tools/test_server.py
    python tools/test_server.py -k enroll        # 额外参数透传给 pytest

0.2.10 的手写端到端断言已迁移为 `tests/` 下的 pytest 用例：
- 协议契约 → `tests/contract/test_protocol_v3.py`
- 验收 A1–A24 → `tests/acceptance/`
- 服务端装配/载体 → `tests/integration/`
原实现保留在安全锚点 tag `pre-rewrite-0.2.10`（spec 头部）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pytest_entry import LAYER_SERVER, run  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run(LAYER_SERVER, sys.argv[1:]))