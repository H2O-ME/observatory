"""pytest 全局配置与沙箱安全临时目录。

硬规则（spec §9）：
- **禁止**使用 `tempfile.mkdtemp()` 的默认系统临时目录：本机沙箱会拒绝其后代目录访问。
- 统一使用工作区内 `<repo>/.pytest-tmp/<uuid>`，该目录已在 .gitignore 中。
  本 conftest 把 pytest 的 basetemp 指向它，并把 TEMP/TMP/TMPDIR 也重定向过去，
  这样子进程（将来 integration 里真起服务端/受控端）不会落到被拒绝的系统临时目录。

使用方式：
    def test_x(workspace_tmp):            # session 级根目录，整个 run 一个
        d = make_tmp_dir(workspace_tmp, "case")   # 每个用例一个子目录
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TMP_ROOT = REPO_ROOT / ".pytest-tmp"
MAX_TMP_AGE_S = 7 * 24 * 3600


def _new_run_dir() -> Path:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / uuid.uuid4().hex
    path.mkdir(parents=True)
    return path


def _prune_old(base: Path) -> None:
    """清理 7 天前的残留，避免无限增长；忽略正在并发使用的目录。"""

    cutoff = time.time() - MAX_TMP_AGE_S
    for entry in base.iterdir():
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def pytest_configure(config: pytest.Config) -> None:
    base = _new_run_dir()
    # pytest 的 tmp_path / tmp_path_factory 都基于 basetemp（在 sessionstart 才初始化）
    config.option.basetemp = str(base)
    # 让子进程也不碰系统临时目录；Windows 用 TEMP/TMP，POSIX 用 TMPDIR
    for name in ("TEMP", "TMP", "TMPDIR"):
        os.environ[name] = str(base)
    tempfile.tempdir = str(base)
    config._observatory_tmp = base  # type: ignore[attr-defined]
    _prune_old(TMP_ROOT)


def pytest_report_header(config: pytest.Config) -> str:
    return f"observatory-tmp: {config.option.basetemp}"


@pytest.fixture(scope="session")
def workspace_tmp(pytestconfig: pytest.Config) -> Path:
    """本次测试运行的沙箱安全根目录（工作区内，唯一）。"""

    base = pytestconfig.option.basetemp
    assert base, "basetemp 未设置"
    path = Path(str(base))
    path.mkdir(parents=True, exist_ok=True)
    # 防御：必须位于仓库内的 .pytest-tmp 下，绝不能是系统临时目录
    assert path.is_relative_to(REPO_ROOT / ".pytest-tmp"), f"临时目录越界: {path}"
    return path


@pytest.fixture()
def tmp_dir(workspace_tmp: Path) -> Path:
    """每个用例独立的临时目录（工作区内）。"""

    return make_tmp_dir(workspace_tmp, "case")


def make_tmp_dir(base: Path, prefix: str = "tmp") -> Path:
    safe_prefix = "".join(ch for ch in prefix if ch.isalnum() or ch in "-_") or "tmp"
    path = base / f"{safe_prefix}-{uuid.uuid4().hex[:12]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


__all__ = ["REPO_ROOT", "TMP_ROOT", "make_tmp_dir"]