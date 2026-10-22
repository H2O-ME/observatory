# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Observatory agent (onedir bundle).

The bundle is intentionally onedir: an auto-update swaps a whole directory, which
is atomic on NTFS, whereas replacing a running onefile executable requires a reboot
or a second stage binary.

`VERSION` from the repository root is bundled so a frozen build reports the same
number as the server and the installer.
"""

from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()
AGENT_DIR = SPEC_DIR.parent
REPO_ROOT = AGENT_DIR.parent
VERSION_FILE = REPO_ROOT / "VERSION"

hiddenimports = [
    # pywin32 pieces imported lazily by the service / pipe / session code.
    "win32timezone",
    "win32serviceutil",
    "win32service",
    "win32event",
    "win32api",
    "win32con",
    "win32file",
    "win32pipe",
    "win32security",
    "win32ts",
    "win32process",
    "win32profile",
    "win32com.client",
    "pywintypes",
    "pythoncom",
    "servicemanager",
    # Runtime-dispatched imports.
    "mss.windows",
    "PIL._webp",
    "httpx",
    "websockets.asyncio.client",
    "observatory_agent.worker",
    "observatory_agent.updater",
    "observatory_agent.uninstall",
    "observatory_agent.service",
]

excludes = [
    "tkinter",
    "unittest",
    "pydoc",
    "doctest",
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "IPython",
    "pytest",
    "sphinx",
    "setuptools",
    "pip",
    "wheel",
]

datas = []
if VERSION_FILE.exists():
    datas.append((str(VERSION_FILE), "."))

analysis = Analysis(
    [str(SPEC_DIR / "entry.py")],
    pathex=[str(AGENT_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="observatory-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="observatory-agent",
)