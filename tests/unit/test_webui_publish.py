"""Console publish contract: an upgraded API must never serve an old SPA.

2026-10-25 生产事故：镜像升级到 0.3.0 后，`/data/webui/.version` 仍是 0.2.10，
旧 SPA 与新 API 混用，登录 200 但立刻跳回登录页、版本号为空。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from observatory import main as main_module
from observatory.core.config import Settings, resolve_version

pytestmark = pytest.mark.unit


def _write_build(root: Path, name: str) -> Path:
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(f"<html>{name}</html>", encoding="utf-8")
    (root / "assets" / f"{name}.js").write_text(name, encoding="utf-8")
    return root


def test_publish_replaces_stale_data_volume_build(tmp_dir: Path) -> None:
    settings = Settings(data_dir=tmp_dir)
    settings.ensure_dirs()
    target = settings.webui_dir
    _write_build(target, "old")
    (target / ".version").write_text("0.2.10\n", encoding="utf-8")

    bundled = _write_build(tmp_dir / "bundled", "new")
    assert main_module._publish_webui(settings, source=bundled) is True

    assert "new" in (target / "index.html").read_text(encoding="utf-8")
    assert (target / ".version").read_text(encoding="utf-8").strip() == resolve_version()
    assert (target / "assets" / "new.js").is_file()
    assert not (target / "assets" / "old.js").exists()
    assert not (settings.data_dir / "webui.old").exists()

    # 幂等：版本已一致时不再搬运。
    assert main_module._publish_webui(settings, source=bundled) is False


def test_resolve_ignores_stale_data_build(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(data_dir=tmp_dir)
    settings.ensure_dirs()
    _write_build(settings.webui_dir, "old")
    (settings.webui_dir / ".version").write_text("0.2.10\n", encoding="utf-8")

    bundled = _write_build(tmp_dir / "bundled", "new")
    monkeypatch.delenv("OBS_WEBUI_DIR", raising=False)
    monkeypatch.delenv("OBS_ENV", raising=False)
    monkeypatch.setattr(main_module, "BUNDLED_WEBUI", bundled)

    path, source = main_module._resolve_webui_dir(settings)
    assert path == bundled
    assert source == "bundled"


def test_resolve_accepts_current_data_build(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(data_dir=tmp_dir)
    settings.ensure_dirs()
    _write_build(settings.webui_dir, "current")
    (settings.webui_dir / ".version").write_text(resolve_version() + "\n", encoding="utf-8")

    monkeypatch.delenv("OBS_WEBUI_DIR", raising=False)
    monkeypatch.setattr(main_module, "BUNDLED_WEBUI", tmp_dir / "does-not-exist")

    path, source = main_module._resolve_webui_dir(settings)
    assert path == settings.webui_dir
    assert source == "data"
