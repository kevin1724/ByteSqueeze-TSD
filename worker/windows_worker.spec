# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the self-contained ByteSqueeze Windows Worker."""

import os
from pathlib import Path

root = Path(SPEC).resolve().parents[1]
tools = Path(os.environ.get("BYTESQUEEZE_WINDOWS_TOOLS") or root / "worker" / "vendor" / "windows" / "tools")
manifest = root / "worker" / "windows_worker.manifest"
version_info = root / "worker" / "windows_version_info.txt"
binaries = []
for name in ("HandBrakeCLI.exe", "ffmpeg.exe", "ffprobe.exe"):
    path = tools / name
    if not path.is_file():
        raise SystemExit(f"Missing required Windows runtime: {path}")
    binaries.append((str(path), "tools"))

datas = [(str(root / "presets"), "presets")]
for name in ("HANDBRAKE-LICENSE.txt", "FFMPEG-LICENSE.txt", "THIRD-PARTY-NOTICES.txt"):
    path = tools / name
    if path.is_file():
        datas.append((str(path), "."))

a = Analysis(
    [str(root / "worker" / "windows_app.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "worker.app",
        "worker.encode_runner",
        "PIL.Image",
        "PIL.ImageDraw",
        "pystray._win32",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ByteSqueezeWorker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(tools / "ByteSqueezeWorker.ico"),
    manifest=str(manifest),
    version=str(version_info),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ByteSqueezeWorker",
)
