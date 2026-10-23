#!/usr/bin/env python
"""真机端到端（对外入口保留，内部调用 tests/integration/，spec §9）。

    python tools/test_e2e_live.py

0.2.10 版本会真起服务端 + 真起受控端（仅 Windows）。0.3.0 的端到端验收在：
- `tests/integration/`（真起服务端 + 假受控端；W1 后换真受控端）
- `tests/contract/test_legacy_v2.py::test_n2_zero_reinstall_upgrade`（N2 零重装升级）
- `tests/acceptance/`（A1..A24）
原实现保留在 tag `pre-rewrite-0.2.10`。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pytest_entry import LAYER_E2E, run  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run(LAYER_E2E, sys.argv[1:]))